# YAML Runtime

YAML V1 assembles a Typeflux Temporal runtime from Python schemas, prompt refs,
and optional Python-defined activities. Hookless AI Activities can be declared
directly in YAML; Python activity modules remain the path for hooks.

> Using the TypeScript SDK? See [YAML Runtime (TypeScript)](typescript/yaml.md).
> The `typeflux.yaml` schema is language-neutral; the TS doc covers the injected
> wiring and the permanent divergences.

## Shape

```yaml
project: examples.support_triage_langfuse
name: support_triage_langfuse
task_queue: support-triage-langfuse-typeflux

runtime:
  temporal:
    address: ${TEMPORAL_ADDRESS:-localhost:7233}
    namespace: ${TEMPORAL_NAMESPACE:-default}
    tls: ${TEMPORAL_TLS:-false}
    api_key:
      value_from:
        env: TEMPORAL_API_KEY
        required: false

  artifacts:
    local_roots:
      - fixtures
    allowed_sources:
      - local_path
      - provider_file
    allowed_media_types:
      - text/plain
      - application/pdf
      - image/*
    max_bytes: 10485760

  registry:
    type: langfuse
    label: production

  provider:
    type: openai
    model: ${TYPEFLUX_OPENAI_MODEL:-gpt-4o-mini}
    params:
      max_tokens: 4096
      timeout: 60
    api_key:
      value_from:
        env: OPENAI_API_KEY

  provider_retry:
    max_attempts: 3
    initial_backoff_seconds: 0.5
    max_backoff_seconds: 5.0
    backoff_multiplier: 2.0
    retry_rate_limits: true
    retry_transient_errors: true

  provider_limits:
    default:
      max_concurrent: 8
      min_interval_seconds: 0.0
    providers:
      openai:
        max_concurrent: 6
        min_interval_seconds: 0.1
        models:
          gpt-4o-mini:
            max_concurrent: 3
            min_interval_seconds: 0.25

  observability:
    type: langfuse
    execution_manifest: true
    redaction:
      enabled: true
      preserve_typeflux_metadata: true

  imports:
    allow_absolute_activity_modules: false
    allow_provider_class: false
    allowed_module_roots: []

activities:
  modules:
    - activities

workflow:
  name: SupportTriageYamlWorkflow
  input: schemas:TicketInput
  output: schemas:ReviewPacket
  steps:
    - id: classification
      activity: classify_ticket
    - id: routing
      activity: route_ticket
```

Langfuse tracing is explicit opt-in for YAML workers and starters. Set
`runtime.observability.type: langfuse` when workflow inputs, outputs, prompts,
and Typeflux metadata should be sent to Langfuse. Langfuse credentials in the
process environment, or `runtime.registry.type: langfuse` for prompt retrieval,
do not enable tracing by themselves. Omitted, `null`, or `type: none`
observability uses a no-op backend.

### LangSmith tracing

`runtime.observability.type: langsmith` exports the same workflow / activity /
generation / hook observations to [LangSmith](https://smith.langchain.com/)
instead of Langfuse. Switching backends is a one-line change — nothing else in
the spec moves.

The LangSmith backend is OpenTelemetry-native: it emits OTEL spans through
Temporal's tracer provider and exports them to LangSmith's OTLP endpoint, so the
native Temporal workflow/activity spans and the Typeflux observation spans
correlate into a single LangSmith trace (per LangSmith's "trace with Temporal"
guide). Configure it through the environment:

- `LANGSMITH_API_KEY` — required.
- `LANGSMITH_PROJECT` — target project (defaults to `default`).
- `LANGSMITH_ENDPOINT` — optional self-hosted/regional base URL.

Install the dependency with `pip install "typeflux[langsmith]"` (pulls
the OTLP exporter via `langsmith[otel]`). The `trace` CLI reads LangSmith back
with `--backend langsmith`:

```bash
typeflux-trace trace list --backend langsmith --json
typeflux-trace trace inspect TRACE_ID --backend langsmith
```

Internally the backend pairs a generic OpenTelemetry writer
(`observability.otel.OtelTraceWriter`) with a LangSmith profile (endpoint, auth,
and span-attribute conventions); a future OTLP-ingesting backend is a new
profile, not a new writer.

## Temporal Connection

`runtime.temporal` configures the Temporal SDK client used by YAML workers and
starters:

```yaml
runtime:
  temporal:
    address: ${TEMPORAL_ADDRESS:-localhost:7233}
    namespace: ${TEMPORAL_NAMESPACE:-default}
    tls: ${TEMPORAL_TLS:-false}
    api_key: ${TEMPORAL_API_KEY:-}
```

For Temporal Cloud, set `TEMPORAL_ADDRESS` to the namespace endpoint,
`TEMPORAL_NAMESPACE` to the namespace id, `TEMPORAL_TLS=true`, and
`TEMPORAL_API_KEY` to an API key. An empty interpolated API key is treated as
unset. If an API key is configured while TLS is disabled, YAML loading fails.

Production YAML can reference secrets without interpolating raw values into the
runtime spec:

```yaml
runtime:
  temporal:
    address: ${TEMPORAL_ADDRESS}
    namespace: ${TEMPORAL_NAMESPACE}
    tls: true
    api_key:
      value_from:
        env: TEMPORAL_API_KEY
```

`value_from.env` reads the named environment variable when the runtime builds
the Temporal client. `value_from.file` reads a mounted secret file. Secret
references are required by default; add `required: false` when an unset source
should behave as unconfigured. Note that `required: false` governs the
**admission/plan presence check** (whether tooling flags the source as a
missing-required secret), not runtime behavior — a `payload_codec` key is the
exception that proves the rule: it **always** fail-closes at runtime on an
unset/short key regardless of `required` (see
[Privacy & Data Protection](privacy.md#payload-codec--encryption-at-rest)).
`project validate` states this wherever it applies (#797): a codec declaring
`required: false` keys gets a `payload_codec_presence` notice naming the
deferred keys and the runtime fail-closed behavior, so the two meanings of
the knob are visible in the report, not only in this callout.

Typed references are the credential contract. A literal string in a secret
field — whether hardcoded or produced by `${VAR}` env interpolation, which is
indistinguishable after loading — puts the credential text into the resolved
spec: the loader logs a warning, deployment generation rejects it, and a
project policy with `secrets.require_secret_references: true` fails admission.
Literal values also appear in secret-reference metadata as
`source_kind: literal` (kind only, never the value) so control-plane surfaces
can flag them.

Observability credentials join the same contract (#793):
`runtime.observability.langfuse.public_key`/`secret_key` and
`runtime.observability.langsmith.api_key` accept literals or `value_from`
references, each block valid only with its own `type:`. The non-credential
fields (`langfuse.host`, `langsmith.endpoint`/`project`) are plain strings —
use `${VAR}` env interpolation for environment-specific values; keeping them
out of the secret contract keeps the inventory exactly the credential list.
Declared fields win field-by-field; unset fields fall back to the standard
env vars (`LANGFUSE_PUBLIC_KEY`, `LANGSMITH_API_KEY`, …), so the blocks are
additive — what they buy is inventory honesty (the credential slots appear in
`bundle.secret_references` and in generated deployment Secret scaffolding)
and admission-checkable references. The required-observability policy gate
(#756) accepts spec-resolved credentials the same as env ones.

TLS gating for the Temporal API key fires at ONE stage regardless of the
authoring shape (#796): a literal key with `tls: false` fails at YAML load,
and a `value_from` reference under `tls: false` is resolved by `project
validate` when its source is visible there — a resolving credential fails the
`temporal_tls_invariant` check exactly like the literal would have, and an
unresolvable source is reported as an explicit deferred notice ("enforced at
client connect"), never silence. Client connect stays the last line for
values that only exist at runtime.

Self-hosted deployments can keep `tls: false` for local/dev clusters or use
`tls: true` when the system trust store is enough. For custom CAs, server-name
overrides, or mTLS, use object form:

```yaml
runtime:
  temporal:
    address: temporal.example.com:7233
    namespace: default
    tls:
      server_root_ca_cert:
        value_from:
          file: /etc/typeflux/temporal/ca.pem
      domain: temporal.example.com
      client_cert:
        value_from:
          file: /etc/typeflux/temporal/client.pem
      client_private_key:
        value_from:
          file: /etc/typeflux/temporal/client.key
```

`client_cert` and `client_private_key` must be configured together. Legacy
`server_root_ca_cert_file`, `client_cert_file`, and `client_private_key_file`
paths remain supported, but `value_from.file` is the preferred production shape
for mounted Kubernetes or Docker secrets. TLS file contents are read by the
worker process when the runtime starts.

Because executions register under versioned workflow types, set
`workflow_search_attribute` to attach the stable logical workflow name as a
Keyword search attribute on every Typeflux-started execution:

```yaml
runtime:
  temporal:
    workflow_search_attribute: TypefluxWorkflow
```

One visibility query then spans all versions of a workflow:

```
TypefluxWorkflow = 'ClaimReviewWorkflow' AND ExecutionStatus = 'Running'
```

The attribute must be registered in the namespace before enabling, or
workflow starts fail. Self-hosted and dev server:

```bash
temporal operator search-attribute create --name TypefluxWorkflow --type Keyword
```

Temporal Cloud: add a Keyword custom search attribute named to match through
`tcld` or the Cloud UI. The attribute applies to `runtime.execute_workflow`
and `project submit` starts; raw `client.start_workflow` calls bypass it, the
same boundary as the identity memo.

## Activity Discovery

There are two activity sources:

- `definitions` for pure prompt-plus-schema AI Activities without hooks
- `modules` for Python-defined activities: `AIActivity` objects (usually when
  hooks are needed) and normal `@temporalio.activity.defn` callables

You can use either source by itself, or both together.

```yaml
activities:
  definitions:
    - name: classify_ticket
      input: schemas:TicketInput
      output: schemas:Classification
      prompt: support/classify
      validation_retries: 2
      artifacts:
        - name: ticket_files
          from: input.attachments
          kind: document
          media_types: [text/plain, application/pdf]
          max_count: 4
          attach:
            role: user
            text: "Attached ticket files:"
    - name: route_ticket
      input: schemas:Classification
      output: schemas:RoutingDecision
      prompt:
        name: support/route
        label: production
        type: chat
```

Mixed projects can keep only hooked activities in Python:

```yaml
activities:
  modules:
    - hooked_activities
  definitions:
    - name: classify_ticket
      input: schemas:TicketInput
      output: schemas:Classification
      prompt: support/classify
```

Relative modules resolve under `project`.

### Output Moderation

A definition can declare a `moderation:` block: after the activity's output is
validated, a **moderator** inspects it and the activity either **blocks**
(fails, non-retryable) or **flags** (records the verdict and continues). Use the
built-in `provider` moderator (no Python) or a custom `moderator` callable:

```yaml
activities:
  definitions:
    - name: classify_ticket
      input: schemas:TicketInput
      output: schemas:Classification
      prompt: support/classify
      moderation:
        provider: openai        # openai | gemini
        on_violation: block     # block (fail-closed) | flag
        # model: omni-moderation-latest   # optional override (provider only)
```

Two built-in providers are available:

- **`openai`** — OpenAI's dedicated moderation endpoint (`omni-moderation-latest`),
  a single classification call over the serialized output.
- **`gemini`** — Gemini has no standalone moderation endpoint, so this issues one
  `generate_content` call (default model `gemini-flash-lite-latest`) that asks the
  model to classify the output into harm-probability bands. **Cost note:** every
  moderated output spends one (small) generation.

The Gemini moderator reports the category vocabulary
`harassment`, `hate_speech`, `sexually_explicit`, `dangerous_content`, plus two
stable pseudo-categories: `prompt_blocked` (the platform blocked the moderation
prompt) and `response_blocked` (the classifier's own output was safety-filtered).
Every category is a fixed, exact-matchable token a policy `semantics.categories`
disallow-list can target; the dynamic block reason (`SAFETY`, `BLOCKLIST`, ...)
is carried in the verdict's `detail`, not the category. An inconclusive
moderation call (no classification parsed and no block signal) raises — the
activity fails closed rather than passing unmoderated output. The signal is a
probability **band**, quantized to a score for `semantics.score_threshold`:

| band | score |
| --- | --- |
| `NEGLIGIBLE` | 0.0 |
| `LOW` | 0.35 |
| `MEDIUM` | 0.65 |
| `HIGH` | 0.9 |

An output flags when any category reaches `MEDIUM` or above (fold sensitivity down
by constructing `gemini_moderator(flag_threshold="LOW")` in Python). Because the
score is banded, a `score_threshold` between two bands (e.g. `0.5`) behaves like
one anchored at the lower band. Both providers set `model` to override the default
generation/moderation model.

```yaml
      moderation:
        provider: gemini
        on_violation: block
        # model: gemini-2.5-flash   # optional override
```

A custom moderator is a `module:callable` returning a `ModerationResult`; because
it executes project code, it is gated by the imports policy — set
`runtime.imports.allow_moderator_callable: true` (and an `allowed_module_roots`
entry for modules outside `project`):

```yaml
      moderation:
        moderator: my_pkg.moderators:safety_check
        on_violation: flag
```

Exactly one of `provider`/`moderator` is required. A project policy
`semantics:` block can mandate moderation (`required`/`require_block`) and apply
disallowed `categories`/`score_threshold` on top — declaring `moderation:` here
lets `project validate` verify a `required` policy **before** deploy. See
[Project Policies](#trusted-operator-boundary).

### Normal Temporal Activities

Module discovery also accepts plain `@temporalio.activity.defn` callables —
from `TYPEFLUX_ACTIVITIES`/`ALL_ACTIVITIES` exports or module globals — so a
non-LLM code step participates in the YAML workflow graph as a first-class
Temporal activity:

```python
from temporalio import activity

from myproject.schemas import Classification, RoutingDecision


@activity.defn(name="route_by_rules")
async def route_by_rules(value: Classification) -> RoutingDecision:
    return RoutingDecision(queue="billing" if value.category == "billing" else "general")
```

The definition must declare exactly one typed input parameter and a typed
return value (both Pydantic models) so workflow graph validation can check the
step input/output chain, and it must have a static activity name. Workflow
steps reference it by name like any other activity. The callable is registered
with the Temporal worker as-is — never through the Typeflux AI wrapper — so it
has no registry/provider coupling, no AI preflight, no output-repair loop, and
no AI observability identity. Execution manifests record it as a planned
activity name; prompt/provider rollup metadata stays AI-only. Plain activities
use the workflow-level defaults for `start_to_close_timeout` and retry policy.
Map steps support AI activities only: map calls pass a `MapActivityContext`
that only the AI activity wrapper accepts, so a map step targeting a plain
Temporal activity fails at workflow generation time.

Use a normal Temporal activity when the step is independent non-LLM code that
deserves its own activity name, timeout, retry policy, and observability
identity. Use an `AIActivity` hook when the Python code post-processes an LLM
output inside the same activity: hooks run within the generated AI activity
wrapper and are not independently schedulable.

## Trusted Operator Boundary

A Typeflux YAML file is trusted operator code, not safe arbitrary tenant input.
Several fields import Python modules, types, or classes, and Python imports can
run import-time code:

- `activities.modules` imports Python activity modules
- `workflow.input`, `workflow.output`, and YAML activity schema refs import
  Pydantic model types
- `runtime.provider.class`, `runtime.registry.class`, and
  `runtime.observability.class` import Python extension classes (`type: custom`)
- `activities.modules[].absolute: true` can import outside the declared
  `project` package

Keep normal application imports project-relative whenever possible:

```yaml
project: examples.support_triage_langfuse

activities:
  modules:
    - activities
```

That resolves to `examples.support_triage_langfuse.activities`.

Use `runtime.imports` for import paths that need explicit operator review:

```yaml
runtime:
  imports:
    allow_absolute_activity_modules: true
    allow_provider_class: true
    allow_registry_class: true
    allow_observability_class: true
    allowed_module_roots:
      - shared_ai
```

By default, project-relative imports are allowed, while absolute activity
modules and every `type: custom` extension class are rejected. The project
package is always an allowed root. External absolute module roots must be listed
in `allowed_module_roots`.

This policy is an application guardrail, not a sandbox. Do not accept raw
user-supplied YAML in hosted or multi-tenant systems without separate review,
isolation, and deployment controls.

### Custom extension classes (`type: custom`)

The provider, prompt registry, and observability backend each accept a
`type: custom` with a `class:` (`module:ClassName`) to plug in your own
implementation of the corresponding protocol:

```yaml
runtime:
  provider:
    type: custom
    class: my_project.providers:MyProvider        # implements ModelProvider
  registry:
    type: custom
    class: my_project.registries:MyRegistry       # implements PromptRegistry
  observability:
    type: custom
    class: my_project.observability:MyBackend     # implements ObservabilityBackend
  imports:
    allow_provider_class: true
    allow_registry_class: true
    allow_observability_class: true
```

Rules:

- `type: custom` requires a `class:`, and a `class:` requires `type: custom` —
  a class on a built-in type is rejected (it would be silently ignored).
- The class is gated by its `runtime.imports.allow_*_class` flag and the
  `allowed_module_roots` policy, exactly like activity modules.
- A custom extension can declare its configuration in the spec with a `config:`
  block — a flat string map whose values are literals or `value_from` secret
  references, resolved at build and passed as `cls(config=resolved)`:

  ```yaml
  runtime:
    provider:
      type: custom
      class: my_project.providers:MyProvider
      config:
        endpoint: https://models.internal
        api_key: { value_from: { env: ACME_API_KEY } }
  ```

  A declared `config` requires the class to accept it
  (`__init__(self, *, config: dict[str, str])`) — silently dropping it would
  read like working configuration that isn't. `value_from` entries are
  required by default (a missing source fails the build); a
  `required: false` entry whose source is absent is simply omitted, and an
  explicit empty literal (`""`) is rejected at load (it means unset
  everywhere else in the secret machinery and would silently vanish). Every
  entry joins the bundle's `secret_references` inventory as
  `runtime.<kind>.config[<key>]` (source kind/name only — values are never
  recorded), profile views mask literal entry values to `***`, generated
  deployments scaffold the `value_from` sources into the worker's
  Secret/env refs, and a policy with `secrets.require_secret_references`
  rejects literal config values like any other literal credential. For
  composed workflows the config block is part of the extension's identity:
  a sub-workflow naming the same custom class with a different config is a
  composition error, never a silent use-the-parent's.
- Without a `config:` block the class is instantiated with **no arguments**; a
  custom extension may still read its own configuration (env vars, files) in
  `__init__`, but those reads are invisible to the secret inventory — prefer
  `config:`.
- A custom provider should set a `provider_name` attribute. It flows to runtime
  metadata, observability, and per-provider rate limiting (without it, the
  identity is derived from the class name). The static policy and admission layer
  identifies a custom provider as `custom` (its YAML type), so it can never
  impersonate a built-in provider's identity — bound a custom provider in a
  project policy via the `custom` identity plus the import-policy allow-list.

A custom provider used to be configured as `type: fake` with a `class:`. That
back door is removed — use `type: custom` instead. `type: fake` now always means
the built-in no-op fake provider.

Prompt refs select a registry prompt one of two ways: `prompt.version` pins an
immutable integer registry version (a Langfuse version number, e.g.
`version: 7`), and `prompt.label` selects a mutable label (e.g.
`label: production` or `label: canary`). They are mutually exclusive; YAML
loading fails when both are set, and a string under `version:` fails with a
pointer to `label:`.

`runtime.registry.label` is the default label for YAML activities that set
neither `prompt.version` nor `prompt.label`, including string shorthand like
`prompt: support/classify`. An explicit per-activity `version` or `label`
always wins over the registry label. With no selector anywhere, resolution
uses the `production` label.

`prompt.type` controls the registry prompt shape. It defaults to `auto`, which
keeps string shorthand compatible and lets the Langfuse registry try text prompts
before chat prompts. Use `type: text` or `type: chat` when a prompt's Langfuse
type is known and you want one direct registry lookup.

Inline YAML prompts can be plain strings or typed chat messages. Message content
can be a string or a list of canonical content parts:

```yaml
runtime:
  registry:
    type: inline
    prompts:
      review_claim:
        messages:
          - role: user
            content:
              - type: text
                text: Review claim {{ claim_id }}.
              - type: artifact_group
                group: claim_documents
                text: "Claim documents:"
```

Portable content part types are `text`, `artifact`, `artifact_group`, and
`provider_extension`. Prefer `artifact_group` when YAML activity inputs can
resolve zero or more files. `artifact` names exactly one resolved artifact by
group name when the group has one item, or by `group[index]` for a specific item.
`provider_extension` is an escape hatch for provider-native payloads and should
be used only inside provider-owned examples or adapters.

Prompt placeholders are `{{ field }}` with dot paths into the activity input
model (`{{ nested.value }}`). Substitution is literal text: field values
round-trip exactly, including `&`, `<`, `>`, and quotes — no HTML or other
escaping is applied. A placeholder that names a missing input field fails the
activity with `missing prompt field`. Mustache sections and partials are not
supported; only validated `{{ field }}` placeholders are substituted.

## Prompt Registries

`runtime.registry.type` selects where prompts come from:

- `inline` — prompts defined under `runtime.registry.prompts` in the YAML.
- `langfuse` — fetched from Langfuse by name and version/label.
- `langsmith` — fetched from LangSmith (see below).
- `custom` — your own `PromptRegistry` class (see
  [Custom extension classes](#custom-extension-classes-type-custom)).

### LangSmith

```yaml
runtime:
  registry:
    type: langsmith
    label: ${LANGSMITH_PROMPT_TAG:-production}   # tag or commit hash
```

LangSmith stores prompts as LangChain serializations. Typeflux parses the
prompt commit's manifest into its chat messages, preserving `system` / `user`
(`Human`) / `assistant` (`AI`) roles. Each message's template is normalized to
Typeflux's `{{var}}` syntax: a `mustache` template passes through unchanged, and
an `f-string` `{var}` template is converted (LangChain's `{{`/`}}` literal-brace
escapes are preserved). The resolved **commit hash** is recorded as the prompt's
`resolved_version`, so it lands in the activity and execution manifests and in
traces.

How it differs from Langfuse:

- **Selection** uses the *same* `label` knob as Langfuse: it resolves a moveable
  **commit tag** (e.g. `production`) — LangSmith's analogue of a Langfuse label —
  or a commit hash. (LangSmith has no integer versions.) You move the tag to a
  new commit the way you'd move a Langfuse label (`push_prompt(commit_tags=…)` on
  a new commit, or re-tagging an existing one — see the contract_risk_review
  `--bootstrap-langsmith` helper). So `label: production` behaves the same on
  both backends. The registry default `runtime.registry.label` applies to prompt
  refs that set neither a version nor a label.
- **Templates** are normalized from LangChain's f-string/mustache formats;
  Langfuse prompts are already mustache.
- **Model config** is not pulled from LangSmith; the workflow's provider model
  applies (Typeflux owns provider selection).

Configure credentials with the LangSmith SDK's standard env vars
(`LANGSMITH_API_KEY`, optionally `LANGSMITH_HOST`). Requires the `langsmith`
extra: `pip install 'typeflux[langsmith]'`.

## Artifacts

`runtime.artifacts` defines the artifact policy for YAML workers:

- `local_roots` are directories that local file artifacts may come from.
  Relative roots resolve against the workflow YAML file.
- `allowed_sources` defaults to `local_path`; supported source kinds are
  `local_path`, `url`, `object_uri`, and `provider_file`.
- `allowed_media_types` can use exact media types or wildcard families such as
  `image/*`.
- `max_bytes` caps resolved artifacts after file size is known.

Activity definitions can extract artifact groups from workflow input:

```yaml
activities:
  definitions:
    - name: review_claim
      input: schemas:ClaimReviewInput
      output: schemas:ClaimReviewOutput
      prompt: review_claim
      artifacts:
        - name: claim_documents
          from: input.documents
          required: true
          kind: document
          media_types: [text/plain, application/pdf]
          max_count: 4
          attach:
            role: user
            text: "Additional claim documents:"
```

Artifact input values can be string paths, artifact mappings, or lists of either.
For local paths, relative values resolve under configured local roots. For
provider-prepared handles, use an explicit source mapping:

```yaml
source:
  type: provider_file
  provider: openai
  file_id: file_abc123
media_type: application/pdf
kind: provider_file
```

Execution manifests include safe artifact provenance: source kind, role, media
type, sha256, and size. They do not include raw file contents, local paths, URLs,
object URIs, provider file IDs, reviewer notes, or freeform artifact text.

`runtime.provider_limits` configures provider/model-specific call limits for the
worker. Policy precedence is exact model, provider, default, then no limit. YAML
limits use the same fields as Python provider controls: `max_concurrent` bounds
simultaneous provider calls and `min_interval_seconds` spaces calls before they
enter the provider SDK.

`runtime.provider_retry` configures short local retries for retryable provider
errors inside one Temporal activity attempt. It maps to `ProviderRetryPolicy`
and accepts `max_attempts`, `initial_backoff_seconds`, `max_backoff_seconds`,
`backoff_multiplier`, `jitter_ratio`, `retry_rate_limits`, and
`retry_transient_errors`. When omitted, provider calls keep the current
no-local-retry default of `max_attempts: 1`.

Retry delays honor the provider's `Retry-After` hint when the SDK exposes it:
the hint floors the configured backoff (even with no backoff configured) and
is never capped down. Up to `jitter_ratio` (default `0.1`, `0` disables)
proportional random jitter is added on top so a fleet of workers hitting the
same rate limit fans out instead of retrying in lockstep.

Provider retry is separate from `validation_retries`, which repairs structured
output validation failures, and from Temporal activity retry, which owns durable
recovery after worker crashes, long outages, or exhausted local provider
retries. Keep YAML provider retry attempts small and bounded.

## Subject Identity (`subjects:`)

A workflow can declare **subject selectors** that pull one or more *subject ids*
off the workflow input at submit time. A subject id is a stable, pseudonymous
handle for the data subject an execution processes (a patient, a claimant, a
trial subject) — the spine of the erasure tooling (#715): every persistence
surface a later erasure operation targets is enumerated by subject id.

> **⚠️ Subject ids are stored in PLAINTEXT — they MUST be opaque pseudonymous
> handles, never raw PHI/PII.** The ids land in three channels that **no payload
> codec and no redaction rule touches**: the `TypefluxSubjectIds` Temporal search
> attribute (readable through the visibility API and the Temporal UI), the
> Langfuse native `userId` + `typeflux.subject:{id}` tags, and the OTel
> `enduser.id` span attribute. Right: an internal surrogate key like
> `subject-0001` your systems map to the person. Wrong: `patient_ref:
> "jane.doe@example.com"` or an MRN — that writes the identifier itself into
> every index. Opacity is not machine-checkable; this contract is the control
> (see [Privacy & Data Protection](privacy.md)).

```yaml
workflow:
  name: EvaluateClaim
  input: schemas:ClaimInput
  output: schemas:ClaimDecision
  subjects:
    - from: input.patient_ref        # required by default
    - from: input.co_subject_refs     # a list value yields several subjects
      required: false                 # optional: contributes nothing if absent
  steps:
    - id: assess
      activity: assess_claim
```

The `from:` selector mirrors an activity artifact input's `from:` — a dotted
`input.<path>` into the validated workflow input. It resolves to a single string
id or a list of them (a review packet spans several subjects). Values are
non-empty strings; order is preserved and duplicates collapse (the **first** id
is the *primary* subject). A **required** selector whose path is missing or empty
is a loud error at start — an un-indexed execution is invisible to erasure, so a
missing subject is a bug, not a silent no-op.

**Explicit override.** The runtime start APIs take a `subject_ids` argument
(Python `runtime.execute_workflow(..., subject_ids=[...])`; TS
`runtime.runWorkflow(client, input, { workflowId, subjectIds: [...] })`) and the
Python submit CLI takes repeatable `--subject`. A **non-empty** explicit override
**wins** over the declarative `subjects:` extraction; an **empty** override is
treated as "no override provided" and falls through to extraction — subjects are
erasure-critical, so there is no "explicitly no subjects" opt-out of a declared
block.

**Where subject ids go.** At start the resolved ids fan to:

- the Temporal **keyword-list search attribute `TypefluxSubjectIds`** — the
  subject→execution index. `TypefluxSubjectIds = '<id>'` (list membership)
  enumerates every execution touching a subject. **A sub-workflow inherits its
  parent's subject ids** (children of a subject's review are that subject's data).
- the **Langfuse** trace: the native `userId` is set to the primary subject and a
  `typeflux.subject:{id}` tag is added per id (native field and portable tags
  always agree).
- the cross-run **cache record**: a `subjects` field on written records (never in
  the cache key/digest, so hit rate is unaffected). A `CacheStore` that implements
  the optional **`SubjectErasableCacheStore`** capability (`erase_subject`, #715
  slice 3) uses these to invalidate a subject's memoized outputs from a write-time
  subject→key index; the reference `InMemoryCacheStore` does. A plain store without
  the capability falls back to a documented full flush. See
  [Retention & Erasure](privacy.md#cache-erasure-subjecterasablecachestore-715-slice-3)
  for the coverage caveat (records written before subject plumbing are invisible).

  Which behavior a deployment actually gets is a wiring choice — so it is
  **declarable and disclosed** (#795) via `runtime.cache_erasure: targeted |
  any`; see [Retention & Erasure](privacy.md#cache-erasure-subjecterasablecachestore-715-slice-3)
  for the enforcement points (runtime assembly, the erase cache surface) and
  the bundle's `erasure` disclosure section.

`TypefluxSubjectIds` is a fixed attribute name (unlike the opt-in
`workflow_search_attribute`) and is stamped whenever an execution has subjects.
Like any custom Temporal search attribute it must be **registered on the
namespace as a `KeywordList` before use** — a deploy-time step:

```sh
temporal operator search-attribute create --name TypefluxSubjectIds --type KeywordList
```

## Temporal Activity Retries And Timeouts

Generated activity calls always carry an explicit, bounded Temporal retry
policy. The built-in default is `maximum_attempts: 5`,
`initial_interval_seconds: 1`, `backoff_coefficient: 2.0`, and
`maximum_interval_seconds: 60` — Temporal's own default would retry a
deterministic provider failure without bound, which for paid LLM calls means
unbounded spend. Configure it at two scopes; the per-activity block wins over
the runtime default, which wins over the built-in default:

```yaml
runtime:
  activity_retry:
    maximum_attempts: 3
    initial_interval_seconds: 1
    maximum_interval_seconds: 60
    backoff_coefficient: 2.0

activities:
  definitions:
    - name: classify_ticket
      input: schemas:TicketInput
      output: schemas:Classification
      prompt: support/classify
      validation_retries: 2
      start_to_close_timeout_seconds: 120
      heartbeat_timeout_seconds: 30
      retry:
        maximum_attempts: 2
```

Set `maximum_attempts: 0` to opt a specific activity (or the runtime default)
back into Temporal-native unlimited retries. Python-defined activities with an
explicit `retry_policy` keep it unchanged.

Three retry layers compose, from innermost to outermost:

1. **Provider retry** (`runtime.provider_retry`) — short local retries for
   rate-limit and transient provider errors inside one Temporal attempt.
2. **Validation repair** (`validation_retries`) — re-prompts the model with
   repair instructions when structured output fails validation, inside one
   Temporal attempt.
3. **Temporal activity retry** (`retry` / `runtime.activity_retry`) — durable
   re-execution after worker crashes, timeouts, or retryable failures.

Worst case, provider calls per workflow step multiply across layers:
`temporal maximum_attempts x provider_retry max_attempts` for transient
errors, with up to `1 + validation_retries` provider calls inside each
Temporal attempt for validation repair. Keep every layer small.

Terminal failure classification: exhausted validation repair, non-retryable
prompt-resolution errors, and non-retryable provider errors (auth, config,
policy) convert to non-retryable Temporal failures — Temporal does not retry
them. Rate-limit and transient provider errors that survive local provider
retries, and user hook exceptions, remain retryable and are bounded by the
activity retry policy.

### Heartbeating and cooperative cancellation

A long provider call would otherwise pin a cancelled workflow until its
`start_to_close_timeout` elapses: the activity never heartbeats, so Temporal
cannot deliver cancellation, and the retry/validation-repair loop has no
checkpoint to abort at. AI activities now heartbeat automatically during the
provider call and abort promptly when the workflow is cancelled:

- Set `heartbeat_timeout_seconds` on an activity to make heartbeating
  meaningful — Temporal then fails over a stuck worker within that window. The
  worker heartbeats at roughly a third of the timeout, around the (blocking)
  provider call.
- Cancellation is observed at cooperative checkpoints: before each
  validation-repair attempt, before each local provider retry, and immediately
  after a retry backoff. A cancel raised at a checkpoint aborts the activity
  instead of spending another attempt.
- A cancel that lands mid-request is observed when that in-flight attempt
  returns (Typeflux does not yet interrupt a single in-flight provider HTTP
  request); the heartbeat keeps Temporal informed in the meantime.

Without `heartbeat_timeout_seconds` the activity still aborts at the cooperative
checkpoints, but Temporal will not fail-fast a worker that is stuck inside a
single provider call.

## Provider Parameters

Provider inference parameters can be configured at workflow, prompt, and
activity scope:

```yaml
runtime:
  provider:
    type: anthropic
    model: ${TYPEFLUX_ANTHROPIC_MODEL:-claude-sonnet-4-6}
    params:
      max_tokens: 16000
      timeout: 90

  registry:
    type: inline
    prompts:
      analyze:
        provider_params:
          temperature: 0
          top_p: 0.8
        messages:
          - role: user
            content: Analyze {{ document_id }}

activities:
  definitions:
    - name: analyze
      input: schemas:InputModel
      output: schemas:OutputModel
      prompt: analyze
      provider_params:
        max_tokens: 24000
        timeout: 300
      start_to_close_timeout_seconds: 600
```

Precedence is specificity-ordered: activity `provider_params`, prompt
`provider_params` or legacy prompt `model`/`temperature`, `runtime.provider`
defaults, then provider built-ins.

The execution model is an exception: YAML/provider configuration is
authoritative by default. A Langfuse prompt config `model`, `provider_model`,
or `provider_params.model` stays visible as prompt metadata
(`typeflux.registry.langfuse.prompt_config`) but does not change the model the
provider call uses, so switching a workflow between provider families does not
require editing or relabeling Langfuse prompts. To let prompt config select
the execution model, opt in explicitly:

```yaml
runtime:
  provider:
    type: anthropic
    model: claude-sonnet-4-6
    allow_prompt_model_override: true
```

With the opt-in enabled, project policy enforcement checks the effective
prompt-selected model. Activity execution manifests and rollups record the
effective model and its origin under `provider_model_source`
(`yaml_provider` or `prompt_config`). If a Langfuse prompt config carries a
descriptive `typeflux.provider_hint` whose `name` does not match the
configured provider, Typeflux logs a sanitized warning and records the
mismatch in the prompt's registry metadata instead of silently ignoring it.

Non-model prompt parameters (`temperature`, `max_tokens`, and the rest)
keep the specificity ordering above regardless of the override setting.

Supported parameters in v1 are `model`, `temperature`, `max_tokens`, `top_p`,
`stop`, `seed`, `timeout`, `top_k`, `frequency_penalty`, and
`presence_penalty`. Validation is provider-aware **at spec load** (#789, both
editions): a configured param the selected built-in provider never maps —
`top_k` for OpenAI, `seed` and penalties for Anthropic — is rejected when the
YAML loads, so `project validate` (CI's offline gate), `admit`, `run`, and
`submit` all refuse the same spec the worker preflight would. `custom`/`fake`
providers skip the load-time check (their provider is injected) and declare
their supported set with `supported_provider_params`, which runtime/preflight
still enforces without editing Typeflux core validation tables.

`max_tokens` caps Anthropic output tokens (constructor default 4096; set
`runtime.provider.params.max_tokens` or per-activity/per-prompt
`provider_params.max_tokens` to raise it). When a response is truncated at
that cap (`stop_reason: max_tokens`), the activity fails immediately with a
non-retryable configuration error naming `provider_params.max_tokens` —
truncated structured output never enters the validation repair loop, which
would otherwise retry into the same cap.

Behavior-shaping parameters (`model`, `temperature`, `max_tokens`, `top_p`,
`top_k`, `stop`, `seed`, and penalties) are part of the workflow contract hash.
Operational parameters such as `timeout` are recorded in the resolved execution
manifest and affect `manifest_hash`, but do not affect `workflow_contract_hash`.
Provider `timeout` controls the provider SDK request deadline. Activity
`start_to_close_timeout_seconds` controls the Temporal activity deadline and
must be larger than the longest expected provider call plus local validation and
hook work. `heartbeat_timeout_seconds` is the maximum gap Temporal tolerates
between heartbeats before failing the attempt over; it should be shorter than
`start_to_close_timeout_seconds`. The `start_to_close_timeout` is recorded in the
resolved execution manifest and affects `manifest_hash`, but does not affect
`workflow_contract_hash`; `heartbeat_timeout` is a worker-side operational knob
and is not part of either hash.

```yaml
activities:
  modules:
    - activities
    - intake.activities
    - review.activities
```

Absolute modules are supported:

```yaml
runtime:
  imports:
    allow_absolute_activity_modules: true
    allowed_module_roots:
      - shared_ai

activities:
  modules:
    - module: shared_ai.compliance.activities
      absolute: true
      include: [classify_ticket]
      exclude: []
```

Discovery order inside each module:

1. `TYPEFLUX_ACTIVITIES`
2. `ALL_ACTIVITIES`
3. all module-level `AIActivity` objects

`ALL_ACTIVITIES` is the recommended project pattern:

```python
ALL_ACTIVITIES = (classify_ticket, route_ticket, draft_response)
```

Duplicate activity names fail during runtime construction.

Typeflux stamps activity provenance during collection:

- YAML definitions are recorded as `definition_source.kind: yaml`
- module-discovered activities are recorded as `definition_source.kind: python`

That source is included in execution manifests, trace inspection, trace export,
and manifest diff output.

## Workflow Generation

YAML V1 generates one Temporal workflow class. Workflows can use ordinary
activity steps, or bounded map steps for moderate in-memory fan-out.

- ordinary activity step input defaults to the previous step output
- map step input comes from its `over` path
- workflow return defaults to the last step output
- `step.id` is required for stable logs, trace metadata, future YAML refs, and UI
- adjacent step types are validated before worker startup

The compact activity-step shape remains valid:

```yaml
workflow:
  name: SupportTriageYamlWorkflow
  input: schemas:TicketInput
  output: schemas:ReviewPacket
  steps:
    - id: classification
      activity: classify_ticket
    - id: routing
      activity: route_ticket
```

## Workflow Versioning And Replay Safety

YAML workflow graphs are immutable deployment artifacts. `workflow.name` is
the stable logical identity, but executions register and start under an
immutable versioned Temporal workflow type derived from a canonical spec
digest, for example `SupportTriageYamlWorkflow.4f1c09ab23de`. Optionally,
declare a human version label instead:

```yaml
workflow:
  name: SupportTriageYamlWorkflow
  version: v7        # registered type becomes SupportTriageYamlWorkflow.v7
```

A version label is a frozen pointer to one graph: when a worker or starter
connects, Typeflux compares the loaded spec digest against the most recent
execution started under the same versioned type and fails if the label was
reused for a different graph. Assign a new label for graph changes.

The spec digest covers the replay-relevant graph shape: workflow name,
input/output types, ordered steps and their activity bindings, map fan-out
shape, and lifecycle review/cancellation semantics, plus the Typeflux
generator version. It deliberately excludes prompt content and versions,
provider type/model/params, timeouts and retries, observability, policy, and
task queue — those change freely without creating a new workflow version.

Because every in-flight execution stays pinned to the workflow type it
started under, Temporal never replays an old history against a changed graph.
Deploy new workers for the new version, let the old version drain (zero
running executions of the old type), then decommission the old workers. The
digest, generator version, and registered workflow type are recorded in the
workflow execution manifest and `typeflux.yaml` trace metadata, and every
start carries a `typeflux_spec_digest` memo so the graph identity is visible
on the execution itself.

### Migrating executions that cannot be waited out

Draining assumes the old version's executions finish on their own. Some do
not — a long-running case, or an execution parked at a review gate that no
one will decide. For those, the supported primitive is **terminate-and-resubmit
with input carry-over**, exposed as the `migrate` control-plane operation and
`typeflux project migrate` CLI. Continue-as-new handoff across graph versions
is deliberately **not** offered: mapping an in-flight interpreter's state
(completed steps, open gates, fan-out progress) onto an arbitrarily edited
graph is exactly the graph-identity problem versioning pins executions against,
and a migration that silently drops or mismaps that state is worse than one
that is honest about restarting from step zero. See the deployment runbook's
"Migrating long-tail executions" for the operator decision tree, idempotency
stance, and review-state-loss semantics.

### Build-id / worker-versioning alignment: evaluated, not adopted

Temporal ships a native worker-versioning surface (`WorkerDeploymentVersion`,
`VersioningBehavior` PINNED/AUTO_UPGRADE in temporalio 1.27 / @temporalio 1.18).
Typeflux does **not** adopt it, and this is a decision on the merits, not a
maturity deferral:

- **Build-ids version workers; Typeflux digests version graphs.** Adopting
  build-ids would not remove the digest axis — graph identity must survive
  regardless, because it is what replay safety, admission, drain semantics, and
  the control-plane contract key on. It would only **add** a second, server-side
  versioning axis to operate.
- **The two editions diverge structurally.** The TypeScript edition's single
  generic workflow type maps naturally onto build-id routing; the Python
  edition's type-per-version scheme already gets "one deployment = one set of
  versioned types" for free, making build-ids largely redundant there. Adopting
  for one edition breaks the cross-edition operational story.
- **The drain machinery is proven and fail-closed.** Replacing visibility scans
  with server-native reachability is an optimization, not a correctness need,
  pre-adoption.

Re-open triggers (recorded so this is not re-litigated from scratch):

1. a consumer needs activity-only rollouts under a pinned workflow version;
2. drain scans hit real scale limits that `EXECUTIONS_SCAN_LIMIT` pagination
   can't cover;
3. Temporal's deployment-version reachability becomes queryable per
   memo-identity (which would serve the TS edition's generic type directly).

Until one of those holds, the deployment planner has no notion of build-ids and
gains nothing from them.

## Bounded Map Steps

Use a map step when one activity should run over each item in a list while
limiting how many Temporal activity tasks are outstanding at once.

```yaml
workflow:
  name: PageReviewWorkflow
  input: schemas:ReviewRequest
  output: schemas:FinalReview
  steps:
    - id: review_pages
      map:
        activity: review_page
        over: input.pages
        concurrency: 4
        collect:
          output: schemas:PageReviewBatch
          field: reviews

    - id: consolidate
      activity: consolidate_reviews
```

`map.activity`, `map.over`, `map.concurrency`, and `map.collect` are required.
There is no default concurrency; set `concurrency` explicitly to a value of at
least `1`.

`over` is a dotted path against workflow context. It can point at workflow input,
such as `input.pages`, or at a prior step output, such as
`classification.items`. The path must resolve to a list field, and the list item
type must match the mapped activity input model.

`collect.output` must resolve to a Pydantic `BaseModel`. `collect.field` must be
a list field on that model, and the list item type must match the mapped
activity output model. `collect.max_bytes` (default `1500000`, `0` disables)
bounds the serialized size of the collected output and fails the workflow
with an actionable error before Temporal's ~2MB per-payload limit produces an
opaque one; because the guard changes control flow for a given history, it
participates in the workflow spec digest. Map results are stored by original index, so the collected
model preserves input order:

```python
PageReviewBatch(reviews=[first_review, second_review, third_review])
```

Consolidation is explicit. Put summarization, ranking, deduping, final review,
or other reduce-style behavior in the next activity. This keeps the fan-out step
small and auditable, while making the consolidation prompt and schema visible.

Map concurrency is a workflow scheduling bound: it controls how many Temporal
activity tasks this one map step starts at a time. It is separate from worker- or
provider-level limits, such as provider call limiters. In production, use both:
map concurrency for per-workflow fan-out shape, and provider limits for shared
capacity across workers and workflows.

Map steps assume a manageable, fully materialized in-memory list. Very large
fan-out, streaming, batching, pagination, partial-success policies, and
hierarchical reduce are intentionally outside this slice.

Schedules and YAML-defined prompt bodies remain outside this slice. Series-parallel
**composition** — parallel branches, `when` gating, sub-workflows, and multiple review
gates — is covered below; it is a statically validated, digest-versioned graph shape,
not a generic signal/query language.

The [`claims_review_composition`](../packages/python/examples/claims_review_composition/)
example exercises every primitive in this section — a parallel block, an if/else
`when` pair, a `workflow:` step, a `map.workflow` fan-out, and two review gates — in
both authoring modes, and runs live end to end.

## Parallel Steps

A `parallel:` step runs several **branches** concurrently over the same running value,
then merges their terminal values into one typed `collect` object (#55). Each branch is
its own nested sequence of steps (any step kinds, including a nested `parallel`).

```yaml
steps:
  - id: classify
    activity: classify_claims
  - id: reviews
    parallel:
      branches:
        - id: assessments
          steps:
            - id: assess_items
              map:
                activity: assess_item
                over: input.claims
                concurrency: 2
                collect: { output: schemas:AssessmentBatch, field: reviews }
        - id: summary
          when: { path: classify.deep_review, eq: true }
          steps:
            - id: summarize_claims
              activity: summarize_deep
      collect:
        output: schemas:ReviewFanout   # fields ARE the branch ids: assessments, summary
  - id: merge
    activity: merge_reviews            # consumes schemas:ReviewFanout
```

- **`collect.output` fields ARE the branch ids** (decision D4 — no renaming layer). A
  branch that can be gated out (has a `when`) must declare its field `Optional`, because
  a skipped branch contributes `None`. `collect.max_bytes` is the map guard (default
  `1500000`, `0` disables) applied to the merged object.
- Branch ids share the workflow's single flat id namespace with step ids.
- **Nesting depth is capped at 3** (a load-time error above it). Deeper nesting is a
  signal the inner block should become a sub-workflow — which also restores operability
  (its own id, status, and drain row). See *Composition governance and ceilings* below.
- Interleaving across branches is not pinned; only each branch's own step order is
  guaranteed. Lifecycle progress counts every leaf step across all branches.

## Conditional Steps (`when`)

Any step — or a parallel branch — may carry a `when:` gate. When it evaluates false the
step is skipped, and for a **top-level** step the remainder of its enclosing sequence is
skipped too (an early exit); inside a parallel branch, only that branch is gated.

```yaml
steps:
  - id: escalate
    when: { path: classify.deep_review, eq: true }   # a leaf predicate
    activity: escalate_decision
```

A `when` is **pure literal data** — either one leaf predicate `{ path, <op>: value }`
or one level of `all:` / `any:` over leaf predicates (deeper nesting is rejected —
decision D1, keeping agent-authored graphs admissible by inspection). `path` is a
dotted workflow-context reference (`input.x` or `<stepId>.field`); operators are `eq`,
`neq`, `lt`, `lte`, `gt`, `gte`, `in`, `exists`.

An **if/else pair** is two parallel branches with complementary predicates — exactly one
runs, and the other's collect field is `None`:

```yaml
parallel:
  branches:
    - id: fast_track
      when: { path: input.priority, eq: low }
      steps: [{ id: auto_ack, activity: acknowledge }]
    - id: full_review
      when: { path: input.priority, neq: low }
      steps: [{ id: triage_all, map: { workflow: claim_triage, over: input.claims, concurrency: 3, collect: { output: schemas:TriageBatch, field: triaged } } }]
  collect: { output: schemas:IntakeFanout }   # fast_track / full_review both Optional
```

A `when` gate's rendered condition is recorded on the `step_skipped` lifecycle event, so
"what could run" (the topology projection) and "what ran and why" join by step id.

## Sub-Workflows (`workflow:` steps and `map.workflow`)

A step may invoke **another workflow of the same project** as a Temporal *child
workflow* (#55). Two forms:

```yaml
workflow:
  name: IntakeWorkflow
  input: schemas:Claim
  output: schemas:AssessmentBatch
  steps:
    - id: assess_claims          # fan a sub-workflow over items (V1 map semantics)
      map:
        workflow: claim_assessment   # a workflow id in typeflux.project.yaml
        over: input.claims
        concurrency: 5
        collect: { output: schemas:AssessmentBatch, field: assessments }
    - id: summarize              # a single sub-workflow invocation
      workflow: summary_pipeline
```

- The `workflow:` value is a **project-manifest workflow id** (`typeflux.project.yaml`),
  not a file path. A standalone spec (loaded without its project) that references
  a sub-workflow is rejected at load — resolution goes through the manifest. This
  works in **both authoring modes**: a pure-YAML project (sibling workflow YAMLs)
  and a YAML+code project (injected schemas/activities), identically.
- A `workflow:` step consumes the running value as the child's `workflow.input`
  and yields its `workflow.output` (the same chaining rule as an activity step,
  typed against the child's declared IO). `map.workflow` fans one child per item
  with the same bounded-concurrency + `collect` + `max_bytes` semantics as an
  activity map.
- **Same-runtime only.** A project resolves under exactly one runtime, so a
  Python parent invoking a TypeScript child (or vice versa) is structurally
  impossible; there is no cross-runtime child seam.
- **Reference cycles are rejected at load.** A workflow must not transitively
  invoke itself (`A -> B -> A`).
- **Activity names are project-wide on a composed worker.** A parent and its
  (transitive) children register on one worker, so an activity name declared by
  more than one of them is allowed only when the definitions are **identical**
  (one activity, declared twice, registers once). Divergent definitions under
  one name are rejected at resolution/assembly, naming both declaring
  workflows — use distinct names or share the definition verbatim.
- **The prompt registry is composed, not duplicated (#748).** A composed worker
  serves ONE runtime registry, built at load as the **merge** of the parent's
  `runtime.registry` and every (transitively) referenced child's — so a child's
  prompts no longer have to be copied into the parent spec. A prompt name in one
  spec is merged in; a name in several is merged once only when the entries are
  **byte-identical** — a genuine conflict is a loud load-time error naming the
  prompt, both source workflows, and the first differing field. This targets
  `inline` registries; for external backends (`langfuse`/`langsmith`) there is
  nothing to merge, but the closure must agree on ONE registry — a child declaring
  a different registry `type` or config (`label`/`host`/`class`) than the parent is
  rejected at load, never silently resolved to the parent's. Both editions behave
  identically.

### Child identity, ids, and lifecycle

Each invocation is a **first-class execution** with its own workflow id, run id,
history budget, memo identity, drain row, and binding verification — the entire
operational surface applies to it unchanged, and it appears in visibility under
its **own** logical workflow name (not the parent's).

- **Deterministic child id**: `{parent_workflow_id}.{step_id}`, and
  `{parent_workflow_id}.{step_id}-{index}` under `map.workflow`.
- **Start options** (both editions): `WorkflowIdReusePolicy: ALLOW_DUPLICATE`
  (a re-run/reset parent re-starts children under the same ids once the prior
  runs have closed; a *still-running* duplicate fails the start loudly rather
  than adopting an orphan) and `ParentClosePolicy: TERMINATE` (a terminated or
  timed-out parent must not leak running children; graceful Typeflux cancellation
  still cancels in-flight child handles first). The child inherits the parent's
  task queue.
- **Correlation**: every child start stamps `typeflux_parent_workflow_id` (the
  immediate parent's workflow id) into its memo, alongside its own identity memo.
  Temporal's `ParentWorkflowExecution` stays the authoritative link. The
  control plane's correlation card lists a run's **direct children** from this
  memo (workflow id, name, status, start time) via a bounded visibility scan.
- A sub-workflow step counts **one** progress unit in the parent's lifecycle;
  the child's interior progress belongs to the child's own lifecycle surface,
  operated directly by its execution id.
- **Bundle projections**: the parent bundle's `topology` is the authoritative
  full-step view — a sub-workflow step appears there as a `workflow` node
  carrying the child's manifest id. The bundle's `steps` array (per-activity
  effective timeouts/retries) **deliberately omits** sub-workflow steps: they
  call no activity of the parent's, and the child's effective options belong to
  the child's own bundle.

### The frozen-label cascade (an accepted cost)

A parent version pins its children's versions: a frozen parent `workflow.version`
denotes **one composed program**. The parent's spec digest folds in each child's
identity (the TS plan embeds the child's resolved plan + digest; Python bakes the
child's versioned type + digest), so a **child-only graph edit moves the parent's
digest — and every ancestor's up the composition chain**. Under frozen labels,
fixing a leaf therefore demands a version-label bump at every ancestor. This is
the versioned-artifact model working as designed; **keep composition shallow**,
and see workflow versioning above for the drain/tail-migration implications.

### Wide fan-outs and the search attribute

The TS edition's frozen-version check scans a bounded page of recent generic-type
executions. A wide `map.workflow` fan-out floods that scan, degrading label
enforcement precisely when graphs get large. Mitigation: configure
`runtime.temporal.workflow_search_attribute` — children stamp their **own**
logical name into it (they are their own workflow), so the parent's scan (already
narrowed to the parent's name) excludes them. **Configuring the search attribute
is strongly recommended for any project that uses sub-workflows.**

### Composition governance and ceilings

A composed program is admitted as **one unit**. Because a parent pins a specific child
plan and digest (the frozen-label cascade above), admitting the parent evaluates the
**transitive closure** of its sub-workflow references: every referenced child's resolved
spec is re-validated against the parent's composed project policy. A child that would
violate the policy — a disallowed provider/model, a missing required review gate, a
broader artifact bound — fails the *parent's* admission, surfaced as the
`policy_subworkflow_closure` check (emitted only for a workflow that references
sub-workflows; a non-composed workflow's validate output is unchanged). This holds in
both editions' `validate` reports, and Python enforces it fail-closed at deploy-time
admission as well. So a strict-policy parent can never delegate to an ungoverned child.

Depth and width are governed by two complementary mechanisms:

- A **hard load-time ceiling** on parallel nesting (depth 3) — see *Parallel Steps*. It
  is not policy-tunable: deeper structural nesting should become a sub-workflow.
- **Policy-configurable composition ceilings** — a `composition` block on a project
  policy (both editions), evaluated as a pure tree-walk over the parsed spec and
  surfaced as the `policy_composition_ceilings` check at every admission point:

  ```yaml
  # a project policy file
  composition:
    # PER-WORKFLOW knobs — evaluated against each closure member individually:
    max_steps: 20              # flattened step count (parallel branch steps included)
    max_parallel_width: 4      # branches in any single parallel block in the workflow
    max_parallel_nesting: 3    # parallel nesting depth; may only TIGHTEN the hard ceiling of 3
    # TREE-WIDE knobs — enforced by the closure walk over the whole composed program:
    max_total_steps: 60        # flattened step SUM across parent + every referenced child
    max_subworkflow_depth: 3   # sub-workflow reference-tree depth (the parent is depth 0)
    allow_map_over_workflow: true   # forbid map.workflow fan-out when false
  ```

  Composed policies merge these most-restrictively: every `max_*` takes the **min**, and
  `allow_map_over_workflow` takes the **AND**, so composition can only tighten a ceiling,
  never widen it. Every ceiling is `>= 1` (0 is never a silent no-op — to forbid
  `map.workflow` fan-out use `allow_map_over_workflow: false`; to forbid a workflow
  entirely, omit it from the validation targets), and a `max_parallel_nesting` above the
  in-spec hard ceiling (3) is a policy-load error.

  The per-workflow knobs bound each member's own graph — every closure member is checked
  individually against the parent's composed policy, so a child that overflows any
  ceiling fails the parent's admission. The tree-wide knobs bound the composed program as
  a whole: `max_total_steps` sums flattened counts over the parent plus every
  transitively referenced child (unique members once), so splitting a large program into
  many small sub-workflows cannot evade the complexity budget; `max_subworkflow_depth`
  rides the same closure walk (measured as the longest reference chain) and names the
  deepest chain when it overflows. Keep composition shallow regardless — the frozen-label
  cascade means depth has a real operational cost (#204).

### Risk tiers

A **risk tier** is a governance *naming + composition* layer over controls that already
exist — not a new enforcement engine (#300). A workflow DECLARES its own tier and the
project policy DEFINES what each tier requires; admission expands the effective tier into
the existing checks, fail-closed.

Declaring an elevated tier is an enforceable intention, and it fails closed
when nothing would enforce it (#788): `validate --environment` fails (and
`run`/`submit`/`migrate`, the project worker, and the control plane — 422 —
refuse at guard build) if a workflow — or anything in its sub-workflow
closure — declares `policy_gated`, `human_gated`, or `prohibited` while no
project policy is selected for the (workflow, environment), the composed
policy declares no `risk_tiers` dimension, or the dimension defines no
requirements (and no denial) for the effective tier. A workflow whose
elevated tier is bound only in *other* environments' targets is reported
(skipped) by `validate` rather than failed — starting it in the unbound
environment still refuses. `safe`/undeclared needs no enforcement and never
fails. The single-spec `yaml.run`/`yaml.submit` path warns instead of failing
— it has no policy machinery by design.

The vocabulary is a fixed, ordered enum: `safe` < `policy_gated` < `human_gated` <
`prohibited`. The order is total (composition and the closure cascade both merge to the
**highest** tier), the tag stays low-cardinality (4 values), and `prohibited` always
denies admission.

- **Declared** — the workflow asserts its own risk in YAML (governance metadata only, so
  it stays OUT of the spec digest — no replay/control-flow effect):

  ```yaml
  workflow:
    risk_tier: human_gated   # optional; unset reads as `safe`
  ```

- **Defined** — a project policy's `risk_tiers` dimension says what each tier *requires*
  and sets a floor. The effective tier is **`max(declared, min_tier)`** — a floor lifts,
  never errors; `require_declared: true` additionally rejects an undeclared workflow
  (regulated projects opt in):

  ```yaml
  # a project policy file
  risk_tiers:
    min_tier: policy_gated       # floor: every workflow is evaluated at >= this
    require_declared: true       # reject a workflow that declares no risk_tier
    human_gated:
      require_review: true       # → an ENABLED lifecycle review gate must exist
      require_moderation: true   # → every AI activity must declare moderation (#158)
      require_redaction: true    # → observability redaction must be on
      require_compensation: true # → every side_effecting activity STEP declares compensate: (#299)
      constrain_providers:       # → provider+model allow-list, SAME shape as providers.allowed
        anthropic:
          models: [claude-sonnet-4-6]
    prohibited: {}               # (any effective `prohibited` denies outright)
  ```

The effective tier's block is a **macro**: each `require_*` expands into the SAME
predicate the standalone check uses (`require_review` → the review-gate check,
`require_moderation` → `_validate_semantics`, `require_redaction` → the observability
redaction requirement), reported under the one `policy_risk_tier` check with each expanded
requirement named. There is one source of truth per control — a tier can never drift from
the dimension it mirrors.

| Macro requirement | Expands to | Satisfied when |
| --- | --- | --- |
| `require_review: true` | the review-gate predicate | an ENABLED `lifecycle` with a resolved review gate |
| `require_moderation: true` | `_validate_semantics` | every AI activity declares `moderation` |
| `require_redaction: true` | the observability redaction requirement | `observability.redaction.enabled` (the built-in redaction; #188 custom rules strengthen it later without changing this surface) |
| `require_compensation: true` | the saga-compensation predicate (#299) | every `side_effecting` activity STEP declares `compensate:` (see *Compensation and rollback*) |
| `constrain_providers: {…}` | the EXACT `providers.allowed` provider+model predicate | the effective provider+model is allowed by the mapping |

`constrain_providers` is deliberately the **same mapping shape** as `providers.allowed`
restricted to models — `{<provider>: {models?: […]}}` — composed by the same allow-list
INTERSECTION and evaluated through the identical provider+model predicate (D300-6), so a
tier-level constraint is exactly as strong as the policy-level one it mirrors. `base_urls`
is deliberately absent (the macro checks provider+model only; accepting a knob it never
checks would be a silent fail-open). Per-tier `require_*` booleans OR-merge on composition
and `min_tier` merges to the highest tier — composition can only tighten.

**The closure cascade.** For a composed program the effective tier **cascades up the
sub-workflow closure: a parent's effective tier is the `max` over itself and every closure
member's.** A `safe` parent that embeds a `human_gated` child is itself at least
`human_gated` — the parent's run executes the child's effects — so the parent must satisfy
the lifted tier's controls (or the closure admission fails). This rides the same walk that
already aggregates `max_total_steps`, and only the LIFT is reported (as
`risk_tier_cascade` on the `policy_subworkflow_closure` check); the parent's own tier is
covered by its `policy_risk_tier` check.

**Surface.** A `RiskTierContributor` stamps redaction-exempt evidence
(`typeflux.risk_tier.{declared, effective, floor_source, satisfied_controls}`) and one
low-cardinality search tag `risk_tier:<effective>` — tier and satisfied-control NAMES
only, never prompt text or secrets. The resolved bundle carries a typed `risk_tier`
(`BundleRiskTier`), omitted entirely when no policy constrains tiers — see *Resolved
Workflow Bundle*. Its `effective` is **always the tier admission enforces**: the
workflow's own `max(declared, floor)`, lifted by the closure cascade when a higher-tier
child raises it — `floor_source` records what set it (`declared` / `policy_floor` /
`cascade:<member workflow id>`), `requirements` is the ENFORCED tier's macro expansion
with per-requirement satisfaction (plus a `require_declared` entry when the policy
demands an explicit declaration — an undeclared workflow under such a policy shows it
unsatisfied rather than reading clean), and the `cascade` block explains a lift (the
lifting member + the lifted tier's re-expansion). Per-activity tiers and positional
gate-placement checks are a deferred follow-up (workflow-level is v1).

### Admission of agent-authored / externally submitted specs

Checked-in YAML is trusted-operator input. A spec authored by an agent or submitted from
outside that trust boundary is admitted through the same governance, extended — not a
parallel system — via `admit_spec` (Python `typeflux.project.admission.admit_spec`
/ CLI `typeflux project admit`; TS `admitSpec` in `@typeflux/temporal-yaml`). It parses
with the bounded loader (`MAX_YAML_BYTES`), resolves the SAME effective runtime the
manifest flow would produce for the target slot (profile composition + environment overlay
+ per-slot overrides — the slot is the explicit `--workflow` argument, else the submitted
spec's own `name`), resolves the composed policy that slot would apply, and runs the
existing check pipeline (policy compliance + composition ceilings + closure admission),
returning a typed `AdmissionReport` of the same per-check shape as `validate`. The report
carries the exact evaluated spec — build the workflow FROM `report.spec` so what runs is
what was admitted, with the admission provenance attached.

Origin is one bit. `origin: "external"` is the hostile-input posture, and the threat
model maps each hostile capability to an existing control:

| Hostile capability | Control |
| --- | --- |
| Code execution via declared imports (`activities.modules`, a custom provider/registry/observability `class:`, a moderator callable) | Structural admission failure for external origin — rejected on spec shape alone, **without importing** (Python check `admission_external_modules_forbidden`; the TS SDK injects these and rejects `class:`/`modules:` at load, so the surface never exists) |
| Code execution via schema refs (every `module:Type` ref — `workflow.input`/`output`, activity `input`/`output`, `collect.output` — imports the named module at graph build, and the `project:` prefix is submitter-controlled) | Each ref's resolved module is validated against the composed policy's `imports.allowed_module_roots` structurally, **without importing** (Python check `admission_schema_ref_roots`; skipped when the policy sets no roots — the external baseline sets them). The TS SDK resolves refs against injected schemas, so the surface never exists |
| Resource exhaustion (steps / width / depth / fan-out) | `composition` ceilings — per-member and tree-wide (`max_total_steps` sums over the closure, so decomposition cannot evade); payloads bounded by the loader limits and `artifacts.max_bytes`; provider spend by `runtime.provider_limits` / `provider_retry` |
| Exfiltration (registry / observability / Temporal hosts, artifact sources) | The existing allow-lists (`runtime.registry.allowed_hosts`, `observability.allowed_backends`, `runtime.temporal.*`, `artifacts.allowed_sources`) |
| Secret capture | `secrets.require_secret_references: true` (secrets never cross the resolver wire) |
| Governance bypass | Review requirements + closure admission quantify over the whole reference tree |

The **recommended external-origin policy baseline** (documented, not hard-coded, so it is
expressed in the same policy YAML as everything else): `imports.allowed_module_roots`
restricted to the project's own schema packages (this also bounds schema refs — set it,
since an unset roots list leaves refs ungoverned) with `allow_absolute_activity_modules:
false` (pure-YAML only), tight `composition` ceilings including `max_total_steps`,
provider/model allow-lists, and `secrets.require_secret_references: true`. An external
submission with **no** governing policy is refused fail-closed. When admission is the
entry point, provenance (`typeflux.admission.spec_origin` / `status` / `policy_hash`,
safe identity only) is stamped via the `AdmissionContributor` when the runtime is built
from `report.spec`; operator filesystem flows never run admission, so their manifests are
unchanged. A control-plane upload endpoint is deferred until a consumer exists.

## Lifecycle Queries, Cancellation, And Review Gates

Lifecycle support is opt-in. It lets application code inspect and control a
YAML-generated workflow through Temporal handles:

```yaml
workflow:
  name: LifecycleReviewWorkflow
  input: schemas:CaseInput
  output: schemas:FinalDecision
  lifecycle:
    enabled: true
    progress: true
    cancellation: true
    history:
      status_event_limit: 50
    review:
      after_step: package_for_review
      invalid_user_decision: warn
      user_decisions:
        escalate:
          route: escalate_case
        approve:
          route: finalize_case
  steps:
    - id: assess_case
      activity: assess_case
    - id: package_for_review
      activity: package_for_review
    - id: escalate_case
      activity: escalate_case
    - id: finalize_case
      activity: finalize_case
```

Generated workflows expose fixed Temporal interfaces:

```python
handle = client.get_workflow_handle("lifecycle-review-demo")
status = await handle.query("typeflux_lifecycle_status")
await handle.signal("typeflux_request_cancel", "user requested cancel")
await handle.signal("typeflux_submit_review", {"user_decision": "approve"})
```

The review signal payload is a `ReviewCommand`: `user_decision` is required and
must match a key under `review.user_decisions`; `reviewer` and `notes` are
optional strings.

Direct Temporal query and signal calls remain valid. When using
`TypefluxYamlRuntime` helper methods for lifecycle status, cancellation, or
review submission, Typeflux also emits first-class lifecycle operation
observations for configured observability backends. These observations are
client-side control-plane metadata, so they are not added to the workflow
execution manifest.

Lifecycle operation observations are meant for explicit user and API actions,
not for wait loops. `query_lifecycle_status(..., trace=False)` runs the same
query without recording a curated observation, and
`wait_for_lifecycle_state(workflow_id, state, timeout_seconds=30.0,
poll_interval_seconds=1.0)` polls untraced until the workflow reports the
requested state (raising `TimeoutError` otherwise). Status polling should be
slow — one to five seconds, or a user-triggered refresh — and clients that
want an auditable status record should make one traced
`query_lifecycle_status` call after the wait returns rather than tracing
every poll.

Progress counts one unit for a normal activity step and one unit per mapped
item. Review gates pause unconditionally after `review.after_step`. Each
submitted `user_decision` resumes the workflow at that decision's `route` step,
and execution falls through from there: the routed step and every later step
run in order. Routes are not mutually exclusive exits. In the example above,
`escalate` runs `escalate_case` and then `finalize_case`, while `approve` skips
`escalate_case` and runs only `finalize_case`. Steps skipped by a forward route
are removed from the progress total so `completed_units` can still reach
`total_units`. Decisions that do not match a `user_decisions` key follow
`invalid_user_decision`: `warn` (the default) records a lifecycle event and
keeps waiting, and `fail` fails the workflow with a non-retryable
invalid-review error. The cancellation signal is cooperative and
records lifecycle metadata; Temporal's built-in workflow cancellation remains
available through normal Temporal APIs.

### Multiple review gates (`lifecycle.gates`)

A workflow declares **either** `review` (one gate — the form above) **or** `gates` (a
list of named gates), never both. `gates` is the multi-checkpoint generalization: each
gate has an `id`, an `after_step`, its own `user_decisions` routes, `invalid_user_decision`,
and optional `timeout`.

```yaml
lifecycle:
  enabled: true
  gates:
    - id: intake_gate
      after_step: consolidate
      user_decisions:
        escalate: { route: escalation }
        expedite: { route: finalize }
      timeout: { seconds: 3600, on_timeout: route, route: finalize }
    - id: compliance_gate
      after_step: escalation
      user_decisions:
        approve: { route: finalize }
        reject:  { route: finalize }
```

- Each gate must follow a **distinct** `after_step` (so at most one gate waits at a time
  in a sequence — routing is never ambiguous), and gate `id`s are unique. A decision
  name reused across gates must route identically (DS4-6).
- The review signal payload gains an optional `gate`: absent with exactly one gate
  waiting resolves that gate (single-gate clients work verbatim); absent with several
  waiting is recorded as an invalid decision rather than guessed. Drive a specific gate
  with `handle.signal("typeflux_submit_review", {"user_decision": "...", "gate": "..."})`.
- The status query's singleton fields keep single-gate semantics bit-for-bit; the
  additive `waiting_gates: [{gate_id, after_step, valid_user_decisions}]` array carries
  the full per-gate truth, and events carry a `gate_id`.
- A gate `timeout` with `on_timeout: route` durably routes to a named forward step when
  no decision arrives (also `fail` or `cancel`).

The [`claims_review_composition`](../packages/python/examples/claims_review_composition/)
example drives two gates by id live.

Lifecycle status is bounded by `workflow.lifecycle.history.status_event_limit`,
which defaults to `50`. The query returns the current lifecycle snapshot and a
recent event tail, plus `event_count`, `events_truncated`,
`oldest_event_sequence`, and `latest_event_sequence` so operators can tell when
the tail is incomplete. Set `status_event_limit: 0` when status callers only
need the snapshot fields and should receive no event tail.

Temporal is the source of truth. Applications can mirror lifecycle status into
their own databases, but Typeflux does not require or manage app persistence.
For audit trails, export Temporal workflow history instead of treating the
status query as durable retention. The lifecycle review example includes an
`audit` command that normalizes workflow, activity, cancellation, and review
events from Temporal history.
Root trace metadata includes safe lifecycle fields such as state, current step,
progress counts, waiting checkpoint, cancellation requested, and terminal
status. Reviewer identities and freeform review notes are not preserved under
`typeflux.*` metadata; audit exports preserve reviewer identity, cancellation
reasons, and failure messages verbatim by default so the export stays usable
as a compliance record, but should still avoid retaining freeform notes unless
the app has an explicit retention policy for them. To redact those sensitive
fields before an export leaves the trust boundary, pass a redactor to
`export_workflow_lifecycle_audit(handle, redactor=RegexPIIRedactor.default())`
or use the lifecycle example's `audit --redact` flag.

## Compensation And Rollback (Sagas)

Durable execution guarantees a workflow *reaches a terminal state* — it does not undo the
external side effects a half-finished run already caused. A workflow that booked a hotel
and then failed to charge a card has a durable failure **and** an orphaned booking. The
saga pattern answers this: each step that causes a side effect declares how to undo it, and
on failure the workflow runs those undos in reverse (#299).

Deployment rollback and business-action rollback are different axes and must not be
conflated. Rolling back a *worker version* (a bad code deploy) is [versioning and replay
safety](#workflow-versioning-and-replay-safety); rolling back a *business action* already
taken by a running workflow is compensation. This section is about the latter.

### Declaring compensation (`compensate:`)

Any activity, `map`, or sub-workflow step (including steps inside `parallel` branches) may
declare a `compensate:` that runs an ordinary, catalog-validated activity to reverse the
completed step:

```yaml
activities:
  definitions:
    - name: book_hotel
      input: schemas:BookingRequest
      output: schemas:Booking
      prompt: reg/book
      side_effecting: true          # the author's declaration: this writes to the world (#299)
    - name: cancel_hotel            # an ordinary activity, used here as a compensator
      input: schemas:Booking
      output: schemas:CancelResult
      prompt: reg/cancel
      side_effecting: true

workflow:
  steps:
    - id: book
      activity: book_hotel
      compensate:
        activity: cancel_hotel      # a normally-declared activity (undeclared name = load error)
        input_from: book            # context dot-path; DEFAULT = the step's OWN output
        # retry: { … }              # optional per-compensation retry override
    - id: charge
      activity: charge_card         # if this fails, `cancel_hotel` runs on `book`'s output
```

- `activity` is a normally-declared activity name; its schema chain is validated against the
  referenced context value at load, so an undeclared name or a type mismatch is a **load
  error**, not a runtime surprise.
- `input_from` is a context dot-path; the default is the compensated step's own output (for a
  `map` step, each completed item's own result). A compensating child workflow
  (`compensate.workflow`) is deferred to v2.

### How the unwind runs (the LIFO)

As each `compensate:`-bearing step completes, the interpreter pushes
`(step_id, activity, resolved_input)` onto a workflow-local stack — push order is the
history-driven completion order, so it is replay-deterministic. On failure the outer catch
walks the stack **in reverse** (last completed, first undone), then re-raises the ORIGINAL
failure. Key properties:

- **One flat LIFO per run.** A failing `parallel` step unwinds compensations recorded by
  *all* completed sibling branches; a failing child workflow unwinds its own stack before
  the failure propagates, and the parent's step-level `compensate:` then covers the
  parent-side effect of having invoked it. `when:`-skipped steps push nothing (the stack
  only ever holds COMPLETED steps).
- **Cancellation also unwinds.** A user-cancelled half-done saga leaves the same orphans as
  a failed one, so `TypefluxWorkflowCancelled` triggers the same reverse walk before the
  cancellation completes.
- **Best-effort but loud.** A compensating-activity failure records a `compensation_failed`
  lifecycle event and the unwind **continues** (one broken undo never strands the rest); the
  terminal `failed`/`cancelled` event then carries `compensation_status: complete | partial
  | none` on the status wire (contract 1.9.0). New `compensation_started/completed/failed`
  events carry the original `step_id`.
- **The non-cancellable caveat.** temporalio exposes no workflow-safe non-cancellable scope
  (`asyncio.shield` is unsafe in the deterministic sandbox), so the unwind runs
  synchronously in the outer failure path and Typeflux's cooperative cancel flag no longer
  fires once the unwind starts. A **native** Temporal cancel (worker shutdown, a Temporal
  terminate) delivered mid-unwind can still interrupt it — the accepted best-effort floor.
  Make compensators [idempotent](#idempotency-and-side_effecting) so a retry after such an
  interruption is safe.

### Idempotency and `side_effecting`

`side_effecting: true` is the author's declaration — same trust model as `cache: reference`
— that an activity writes to the world (a downstream write, a notification, a charge). It is
pure **governance metadata**: it never enters the workflow digest (definitions are not
digest inputs), so marking an activity side-effecting leaves the registered workflow type
byte-identical, exactly like `moderation` and `risk_tier`.

The flag exists so governance can *require* compensation (below) and so authors mark the
activities that most need **idempotency**. Because both retries and the best-effort unwind
can run a side-effecting activity more than once, an external write should carry an
idempotency key so a duplicate delivery is a no-op:

```yaml
- name: charge_card
  input: schemas:ChargeRequest      # carries a caller-supplied idempotency_key
  output: schemas:ChargeResult
  prompt: reg/charge
  side_effecting: true
```

The activity implementation passes `idempotency_key` to the payment API; a replay or
compensation retry with the same key returns the original result instead of double-charging.
The **idempotent external activity** example (`compensation_saga`) is runnable and shows the
key threaded end to end.

### Review before side effect (the safest pattern)

The strongest guard against an *unwanted* side effect is to never take it unattended: gate
the side-effecting step behind a [lifecycle review gate](#lifecycle-queries-cancellation-and-review-gates)
so a human approves before the irreversible action, not after. Compensation is the recovery
net when a side effect that *did* fire must be undone; a review gate is the prevention that
keeps it from firing wrongly in the first place. Prefer prevention, and keep compensation for
the failures no gate can foresee:

```yaml
activities:
  definitions:
    - name: disburse_payment
      # ...
      side_effecting: true          # the declaration lives on the DEFINITION, not the step

workflow:
  lifecycle:
    enabled: true
    review:
      after_step: assess            # human reviews the AI assessment …
      user_decisions:
        approve: { route: disburse } # … BEFORE the side-effecting disbursement
  steps:
    - id: assess
      activity: assess_claim
    - id: disburse
      activity: disburse_payment
      compensate:
        activity: reverse_payment   # the net, for the failures the gate can't foresee
```

Review gates are one-shot and fire only while the lifecycle is enabled; a fired gate never
reopens, and the unwind runs unattended (approval-*before-compensation* is a deferred v2
gate inside the compensation path, not designed around today). See the runnable
**review-before-side-effect** example (`review_before_side_effect`).

### Terminate and resubmit (the operator escape hatch)

When a run is wedged in a way compensation cannot resolve — a poisoned input, a bug fixed in
a newer spec — the operator escape hatch is to terminate the stuck execution and resubmit
the work against the corrected workflow. This is the same [migrate flow used for executions
that cannot be waited out](#migrating-executions-that-cannot-be-waited-out) (#204): drain or
terminate the old execution, then start a fresh one on the new version. Prefer compensation
for *foreseeable* rollbacks (it is plan-native and audited); reserve terminate-and-resubmit
for *unforeseen* wedges where continuing the current run is not the goal.

### Governance: `require_compensation`

A `risk_tiers` tier can *require* the saga discipline. `require_compensation: true` (an
OR-merged `require_*` macro, see [Risk tiers](#risk-tiers)) fails admission unless **every
`side_effecting` activity STEP in the workflow declares `compensate:`** — a plain `activity`
step or a `map` fanning a side-effecting activity. Sub-workflow steps are out of scope (the
child governs its own effects), and the requirement rides the same closure cascade as the
other macros: a `require_compensation` tier lifts through sub-workflows exactly like
`require_review`. It reports under the one `policy_risk_tier` check with the requirement
named.

### Manifests and provenance

Compensations are **planned graph steps**, so recording them satisfies the "execution
manifests do not record ad hoc client-side rollback actions unless they are planned workflow
graph steps" criterion by construction. A Python `CompensationContributor` (the
`RiskTierContributor` template) stamps redaction-exempt evidence
`typeflux.compensation.{declared_steps, status}` — the ids of the steps that plan a
compensation plus a static `declared` marker, both low-cardinality identity, never
compensation inputs or secrets — and one search tag `typeflux.compensation:declared`, only when the
workflow actually declares compensation (a non-saga workflow's manifest is byte-unchanged).
This `status` is the design-time marker (the plan carries compensations); it is distinct
from the runtime terminal `compensation_status` (`complete`/`partial`/`none`) on the status
wire, which is known only after an unwind executes. Like `RiskTierContributor`, the
contributor is Python-only — the TS SDK has no workflow-metadata contributor seam yet, so TS
records compensation identity through the plan node and status wire; enforcement
(`require_compensation`) is at full parity across editions.

## Worker vs Starter

The worker listens on a task queue and executes workflows and activities:

```bash
cd packages/python
uv run python -m typeflux.yaml.run examples/support_triage_langfuse/typeflux.yaml
```

The starter submits workflow executions:

```python
runtime = await build_runtime(load_yaml_spec("examples/support_triage_langfuse/typeflux.yaml"))
result = await runtime.execute_workflow(ticket, id="support-ticket-123")
```

For a CLI-driven observable start, submit through the Typeflux YAML submit
entrypoint:

```bash
uv run python -m typeflux.yaml.submit examples/support_triage_langfuse/typeflux.yaml \
  --input ticket.json \
  --workflow-id support-ticket-123 \
  --tag live-smoke
```

In production, workers are long-running processes. API servers, jobs, or other
launchers submit workflows through `runtime.execute_workflow(...)` or
`typeflux.yaml.submit` when they want Typeflux root workflow
observability with input/output, tags, YAML metadata, and execution manifests.
Raw Temporal starts are still supported; with YAML observability enabled, the
worker emits correlated activity/generation observations that include workflow
ID, run ID, task queue, workflow type, activity type, and activity ID, but raw
starts do not create a Typeflux root workflow trace.

For container, Docker Compose, Kubernetes, scaling, retry, shutdown, and
debugging guidance, see [YAML Worker Deployment](yaml-worker-deployment.md).

## Project Manifests

Workflow YAML remains the concrete runtime contract. A project manifest is an
optional discovery layer for serious projects with many workflows:

```yaml
version: "1"
name: claims-platform

defaults:
  workflow_filename: typeflux.yaml

workflows:
  - id: insurance_claim_review
    directory: workflows/insurance_claim_review
  - id: support_triage
    path: workflows/support_triage/typeflux.yaml

environments:
  local: environments/local.yaml
  temporal_cloud_dev: environments/temporal-cloud-dev.yaml

policies:
  base: policies/base.yaml

validation:
  targets:
    local:
      workflows: [insurance_claim_review, support_triage]
      environment: local
      policies: [base]
```

Project paths are relative to the project manifest unless absolute. Directory
workflow entries resolve to `<directory>/<defaults.workflow_filename>`.

### Component Profiles

Profiles are reusable, file-referenced runtime fragments so provider, registry,
and runtime settings become selectable components instead of repeated YAML
blobs. Three kinds exist, each owning a disjoint subtree:

- `provider` may set only `runtime.provider`
- `registry` may set only `runtime.registry`
- `runtime` may set only `runtime.temporal`, `runtime.observability`,
  `runtime.provider_retry`, and `runtime.provider_limits`

```yaml
profiles:
  provider:
    anthropic-prod: profiles/provider/anthropic-prod.yaml
  runtime:
    hardened: profiles/runtime/hardened.yaml

workflows:
  - id: insurance_claim_review
    directory: workflows/insurance_claim_review
    profiles:
      provider: anthropic-prod
      runtime: hardened
```

A profile file is typed and versioned:

```yaml
version: "1"
name: anthropic-prod
kind: provider
runtime:
  provider:
    type: anthropic
    model: claude-sonnet-4-6
    api_key:
      value_from:
        env: ANTHROPIC_API_KEY
```

Selections allow at most one profile per kind. Environment profiles can swap a
selection per workflow (whole-reference replacement, no partial mixing):

```yaml
# environments/local.yaml
workflows:
  insurance_claim_review:
    profiles:
      provider: fake-local
```

Resolution precedence is one deterministic pipeline with one merge rule — the
same override-wins deep merge environments already use:

```
workflow YAML  <  selected profiles  <  environment overrides
```

Profiles ride the same override allowlist as environments, scoped further to
their kind's subtree, so a profile can never touch `task_queue`, `artifacts`,
or `imports`. Unknown references, kind mismatches, and out-of-subtree keys
fail loudly at validation and resolution. Profiles use the same typed
`value_from` secret references as workflow YAML; literal credentials get the
same warnings and rejections.

Each applied profile is recorded as safe component provenance — kind, id,
name, content hash (canonical-JSON sha256), source path, and the override
paths it set — on the resolved workflow, in the resolved workflow bundle's
`components` field, and (ids and hashes only) under `typeflux.components` in
workflow metadata and execution-manifest contributions. Provenance never
carries secrets, prompt text, or configuration values.

Use the project CLI to list and validate references (`typeflux-project` is the
installed console script, #810; `python -m typeflux.project` remains
the equivalent no-scripts spelling — likewise `typeflux-controlplane` and
`typeflux-trace`):

```bash
cd packages/python
uv run typeflux-project list typeflux.project.yaml
uv run typeflux-project environments typeflux.project.yaml
uv run typeflux-project validate typeflux.project.yaml
uv run typeflux-project validate typeflux.project.yaml --json
```

Exit codes follow one contract across the project CLI (#818), so CI can
distinguish failure classes from the code alone:

- `0` — success.
- `1` — operational failure (unreachable backends, profile/runtime errors), or a
  negative *status* verdict (`drain-status` not drained, `erase` receipt failed).
- `2` — usage error (bad flags/arguments).
- `3` — validation/admission/drift verdict: invalid manifest, project or bundle
  validation failure, `admit` REJECTED, `deploy --apply` plan drift, and
  `--expect-policy-hash` drift (a malformed or self-conflicting hash *input* is
  a usage error and exits `2`). The TS CLI emits `3` for the plan-drift verdict;
  its invalid-manifest classification still exits `1` (typed loader errors
  pending — noted in the TS docs).

`validate` can also perform environment-aware validation for CI. This resolves
the selected environment profile, applies its variables and workflow overrides,
imports activity and schema definitions, validates workflow graph types, and
constructs the offline execution manifest shape without starting a worker or
connecting to Temporal:

```bash
uv run typeflux-project validate typeflux.project.yaml \
  --environment local

uv run typeflux-project validate typeflux.project.yaml \
  --environment temporal_cloud_dev \
  --workflow insurance_claim_review \
  --policy regulated \
  --json
```

If `--environment` is provided without `--workflow`, every workflow in the
project manifest is validated against that environment. `--workflow` may be
provided more than once to validate a subset. `--policy` may also be provided
more than once. When `--policy` is omitted, validation applies policies from
matching `validation.targets` entries for the selected environment/workflow. If
no policy matches, resolved workflow validation still runs and records policy
enforcement as skipped. `--policy` requires `--environment` because policy
admission is evaluated against a resolved environment/workflow bundle.

Policy files are typed project inputs. Project validation loads them through the
policy schema, validates local `extends` references, and can compose selected
policies into a stable policy hash. Inheritance is restrictive for allow-lists:
when a parent and child both set an allow-list, the effective list is the
intersection. An absent allow-list is unrestricted; an explicit empty allow-list
is deny-all. For example, omitting `providers.allowed` allows any configured
provider, while `providers.allowed: {}` allows no providers. A provider-specific
empty mapping such as `providers.allowed.openai: {}` allows that provider with
unconstrained models. Two non-empty allow-lists that have no overlap are rejected
as a policy composition error rather than silently becoming deny-all; this
usually means two selected policies contradict each other. Use an explicit empty
list or mapping when deny-all is intentional. Booleans compose by polarity
(`require_*`/`*_required` OR together, `allow_*`/`retry_*` AND together).
Numeric scalars with a well-defined monotonic direction merge to the most
restrictive value: `artifacts.max_bytes` and `max_concurrent` take the
minimum, `min_interval_seconds` takes the maximum. Other conflicting numerics —
the retry/backoff fields, which have no clean "stricter" direction — are
rejected so an operator resolves them explicitly. Validation is offline: it does not
start workers, connect to Temporal, or call providers. Project `run` and
`submit` apply the same admission checks before starting workers or submitting
workflows, and the YAML runtime also checks prompt-resolved provider models
before provider calls.
Policy-governed runs record safe policy identity in workflow metadata and execution
manifest contributions.

Example policy:

```yaml
version: "1"
name: regulated
extends:
  - base

providers:
  allowed:
    openai:
      models:
        - gpt-4.1
        - gpt-4o
      base_urls:
        - https://llm-gateway.internal/v1
    anthropic:
      models:
        - claude-sonnet-4-6

observability:
  required: true
  allowed_backends: [langfuse]
  redaction:
    required: true
    preserve_typeflux_metadata: true

runtime:
  temporal:
    allowed_regions: [us-east]
    address_regions:
      us-east.tmprl.cloud:7233: us-east
    require_tls: true
    require_api_key: true
  registry:
    allowed_hosts:
      - https://us.cloud.langfuse.com

artifacts:
  allowed_sources: [local_path]
  allowed_media_types:
    - application/pdf
    - image/*
  max_bytes: 20971520

review:
  require_review_routes: true
  invalid_user_decision: fail

secrets:
  require_secret_references: true

imports:
  allow_provider_class: false
  allow_absolute_activity_modules: false
  allowed_module_roots:
    - company_workflows
```

Policy YAML should contain constraints and requirements, not credentials or
environment values. Environment profiles resolve values such as namespace,
region, task queue, TLS/API-key posture, provider config, and observability
backend; policies decide whether those resolved values are acceptable.

Endpoint constraints: `providers.allowed.<provider>.base_urls` restricts
`runtime.provider.base_url` (an unset `base_url` targets the provider's
official default endpoint and is always allowed), and
`runtime.registry.allowed_hosts` restricts the effective prompt registry host,
including a host resolved from `LANGFUSE_HOST` when the spec leaves it unset.
Both prevent a YAML edit from redirecting provider traffic or prompt-registry
credentials to an unapproved endpoint.

Region assurance: `runtime.temporal.address_regions` maps concrete Temporal
addresses to regions. When present, the policy mapping is authoritative — the
spec address must be mapped, the mapped region must satisfy
`allowed_regions`, and a conflicting self-attested `TYPEFLUX_TEMPORAL_REGION`
fails validation. Without the mapping, `allowed_regions` falls back to
checking the self-attested environment variable.

Environment profiles resolve `project + environment + workflow` into a concrete
runtime spec:

```yaml
version: "1"
name: local

env_files:
  - path: ../.env
    required: false

variables:
  TYPEFLUX_ENVIRONMENT: local
  TYPEFLUX_DEPLOYMENT_ID: local-dev
  TYPEFLUX_TEMPORAL_REGION: local

overrides:
  runtime:
    temporal:
      address: localhost:7233
      namespace: default
      tls: false
      api_key: ""
    observability:
      type: none

workflows:
  insurance_claim_review:
    overrides:
      task_queue: insurance-claim-review-local
```

Cloud profiles can keep API keys out of resolved YAML by using typed secret
references:

```yaml
overrides:
  runtime:
    temporal:
      address: ${TEMPORAL_ADDRESS}
      namespace: ${TEMPORAL_NAMESPACE}
      tls: true
      api_key:
        value_from:
          env: TEMPORAL_API_KEY
    provider:
      api_key:
        value_from:
          env: OPENAI_API_KEY
```

Use `ANTHROPIC_API_KEY` instead when `runtime.provider.type: anthropic`.
Built-in provider defaults are materialized into resolved YAML for audit and
policy admission: OpenAI defaults to `gpt-4o-mini`, and Anthropic defaults to
`claude-sonnet-4-6`. Examples may override those through
`TYPEFLUX_OPENAI_MODEL` or `TYPEFLUX_ANTHROPIC_MODEL`.

Project environment commands:

```bash
cd packages/python
uv run typeflux-project resolve typeflux.project.yaml \
  --workflow insurance_claim_review \
  --environment local \
  --json

uv run typeflux-project run typeflux.project.yaml \
  --workflow insurance_claim_review \
  --environment local \
  --policy regulated

uv run typeflux-project submit typeflux.project.yaml \
  --workflow insurance_claim_review \
  --environment temporal_cloud_dev \
  --input input.json \
  --workflow-id claim-review-001 \
  --subject subject-0001 \
  --policy regulated
```

`--subject` (repeatable, #805) associates the run with subject id(s) for the
erasure index with the yaml edition's exact semantics: an explicit override
wins over the spec `subjects:` extraction.

A CLI-only operator also gets the lifecycle verbs the control plane serves
(#802) — the same pinned-runtime enforcement and gate semantics:

```bash
uv run typeflux-project status typeflux.project.yaml \
  --workflow insurance_claim_review --environment local \
  --execution-id claim-review-001            # add --trace for an auditable check

uv run typeflux-project review typeflux.project.yaml \
  --workflow insurance_claim_review --environment local \
  --execution-id claim-review-001 \
  --decision approve --gate legal --reviewer ops@example

uv run typeflux-project cancel typeflux.project.yaml \
  --workflow insurance_claim_review --environment local \
  --execution-id claim-review-001 --reason "duplicate run"
```

Reviewer identity, notes, and cancel reasons are sent as Temporal signal
payloads and persist in workflow history (#325) — send only data appropriate
to retain there.

Allowed environment overlays are intentionally narrow: `task_queue` and
`runtime.temporal`, `runtime.registry`, `runtime.provider`,
`runtime.provider_limits`, `runtime.provider_retry`, and
`runtime.observability`. Environment profiles cannot change workflow graph,
activity definitions, YAML `project`, or YAML `name`.

Project commands apply environment profiles in a scoped process environment:
listed `env_files` load first, then profile `variables` override those values.
The workflow YAML plus allowed profile overlays are interpolated after that.
Secret values may live in ignored env files or the existing process
environment; `resolve --json` reports only safe booleans such as
`api_key_configured`, never secret values or env-file contents.

When environment or loader overrides are applied, Typeflux records safe override
provenance in workflow metadata and execution manifests. The recorded payload
contains the project/environment/workflow context and overridden field paths such
as `task_queue` or `runtime.temporal.address`; raw override values, API keys, and
certificate contents are not recorded under `typeflux.*`.

When typed secret references are present, Typeflux records
`typeflux.secret_references` and
`typeflux.execution_manifest.contributions.secret_references` with only the
runtime path, source kind (`env` or `file`), source name/path, and a configured
boolean. Raw API keys, certificate contents, and token values are never recorded
or emitted as search tags.

Offline policy admission validation is available through
`typeflux-project validate`. Project `run` and `submit` apply
the same policy admission before runtime work begins. When a project policy is
applied, Typeflux records `typeflux.policy` and
`typeflux.execution_manifest.contributions.policy` with only safe policy audit
fields. Trace metadata includes policy version, selected/applied policy IDs,
policy names, policy hash, `admission_status`, and `enforcement_mode`.
`enforcement_mode` identifies the entrypoint that admitted the workflow, such as
`project_submit` or `runtime`; project `run` records worker-side guard
provenance because it starts a worker rather than submitting a root Typeflux
workflow trace. The execution manifest contribution keeps stable policy identity
plus `admission_status` and intentionally omits `enforcement_mode`, so policy
identity does not vary by launch command. `manifest_hash` may still vary for
real execution/deployment fields such as workflow ID, run ID, task queue, code
provenance, Temporal connection, or other manifest inputs.

`admission_status` is `passed` on workflow traces and manifests because failed
policy admissions block before workflow start. Failed or blocked admissions are
reported through project validation reports and CLI errors instead. Raw policy
contents, descriptions, credentials, environment values, reviewer text, and TLS
paths are not preserved under `typeflux.*`. Policy hashes are metadata-only;
they are not emitted as search tags.

## Resolved Workflow Bundle

Control-plane clients consume one project/environment/workflow selection as a
single immutable, secret-safe JSON payload instead of re-implementing project
resolution:

```bash
cd packages/python
uv run typeflux-project bundle typeflux.project.yaml \
  --workflow claim_review \
  --environment temporal-cloud-dev \
  --policy regulated
```

The bundle (`bundle_version: "1"`, also available as
`typeflux.project.resolve_workflow_bundle(...)`) composes existing
surfaces — project resolution, validation, policy composition, workflow
identity, activity collection, and secret references — and contains:

- project and environment identity (env-file statuses and variable names only)
- workflow identity: logical name, registered versioned workflow type, spec
  digest/algorithm/generator version, input/output schema identities
- resolved temporal/registry/provider/observability settings with
  secret-bearing fields reduced to configured/not-configured booleans
- selected/applied policy ids, names, and the composed policy hash
- the workflow's `risk_tier` posture under the composed policy — `effective`
  is always the tier admission ENFORCES (cascade-lifted when a higher-tier
  child raises it; `floor_source` then reads `cascade:<member>`), with the
  enforced tier's per-requirement satisfaction (incl. `require_declared` when
  the policy demands a declaration) and the explanatory closure `cascade`
  block — omitted when no policy constrains tiers; see *Risk tiers*
- activity descriptors (kind, schema identities, prompt ref, source, declared
  timeout/retry, artifact definitions, step usage)
- steps with their **effective** resolved Temporal timeout and retry policy
- lifecycle review gate configuration and the valid `user_decisions` map
- secret references (path, source kind/name, configured) — never values
- the full project validation result (`ok`, issues, checks); a bundle that
  fails validation still resolves so clients can render why
- an optional deployment plan preview when `--deployment-image` is provided
  (config key names only, never values), or the generating `deploy` command
  as a reference otherwise

The same inputs always produce identical JSON. The `components` field is
reserved for future component-profile provenance. The CLI exits non-zero when
the bundle fails validation.

## Activity Catalog

`typeflux.project.resolve_activity_catalog(project, workflow_id=…,
environment_id=…)` returns the discovered activities for one resolved
workflow/environment in a UI-friendly, secret-free shape — the composition
surface for the control plane:

- AI activities and normal Temporal activities are clearly distinguished
  (`kind: ai` / `kind: temporal`), with AI-only metadata (secret-safe prompt
  ref, provider params, validation retries, artifact inputs) kept separate
  from plain activity metadata.
- Input/output contracts carry both the schema identity (name + hash) and the
  full JSON Schema for rendering.
- `compatible_next` lists the activities whose input type matches each
  activity's output type — statically valid successors for composition —
  and `used_by_steps` maps activities back to the resolved workflow's steps.

Discovery runs through the same import-policy and duplicate-name validation
as the runtime, and the catalog never contains prompt text or secret values.

```bash
cd packages/python
uv run typeflux-project catalog typeflux.project.yaml \
  --workflow insurance_claim_review \
  --environment local
```

## Workflow Operations

`typeflux.project.WorkflowOperations` is the control-plane API for
operating workflows from a project-local UI or service: start, status, review,
and cancel, wrapped in UI-safe DTOs. These are control-plane actions, not
execution-graph data — they never enter workflow execution manifests, and
reviewer identity, freeform notes, and cancellation reasons stay out of
`typeflux.*` metadata. They are still sent as Temporal signal payloads, though,
so they persist in workflow history (and the cancel reason is returned to
`inspect` callers via status) — they are not client-side (#325).

```python
from typeflux.project import WorkflowOperations, load_project_spec

project = load_project_spec("typeflux.project.yaml")
ops = await WorkflowOperations.for_project_workflow(
    project,
    workflow_id="claims",
    environment_id="cloud",
    policy_ids=("regulated",),
    expected_policy_hash=deployed_policy_hash,  # fails closed before connecting
)

receipt = await ops.start(claim_input, workflow_id="claim-1042")
# receipt: workflow_id, run_id, versioned workflow_type, spec_digest,
# task_queue, and a trace_query_hint usable with TraceListQuery.

status = await ops.status("claim-1042")
# status.status is the lifecycle snapshot; status.valid_user_decisions maps
# each valid review decision to its route target for this resolved version.

await ops.submit_review("claim-1042", {"user_decision": "approve"})
await ops.request_cancel("claim-1042", "duplicate claim")
```

`for_project_workflow` mirrors `project submit` construction — environment
profile, policy guard, and expected-policy-hash verification before any
Temporal connection. `ops.start` is non-blocking: it applies the same
identity memo and logical-name search attributes as `execute_workflow` and
returns identity immediately, but opens no root workflow observation (root
traces come from `execute_workflow`/`project submit`; worker-side activity
observations still correlate by workflow id). `ops.status` polls untraced by
default so UI refresh loops never flood the audit trail — poll at
`RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS` (1s) or slower, or use
user-triggered refresh, and pass `trace=True` for a deliberate auditable
check. Review and cancel are explicit user actions and are always recorded as
curated lifecycle operations. Advanced callers can reach the underlying
runtime and raw Temporal handles via `ops.runtime`.

## Runtime Components

Registry types:

- `inline` for tests and demos
- `langfuse` for Langfuse prompt resolution
- `langsmith` for LangSmith prompt resolution
- `custom` for a project-supplied registry extension

Provider types:

- `fake` for tests and demos
- `openai` for structured OpenAI calls through Instructor JSON Schema mode
- `anthropic` for structured Anthropic Messages calls through provider-native
  structured output
- `gemini` for structured Gemini calls (`google-genai`, the `gemini` extra)
- `custom` for a project-supplied provider extension

Every provider accepts an optional `structured_mode` field pinned to its one
legal value, `json_schema` — a forward-compatibility constraint pin, not a
switch. Nothing branches on it today (all providers already do schema-native
structured output); its job is to make a typo'd or aspirational mode fail
validation loudly instead of being silently ignored, and to reserve the field
name for a future second mode.

Observability types:

- `none`
- `langfuse`
- `langsmith`
- `custom` for a project-supplied observer extension

If Langfuse is selected, Typeflux configures Langfuse tracing, Temporal
OpenTelemetry spans, Typeflux activity/generation/hook observations, root
workflow input/output, execution manifests, and default regex PII redaction.

## Environment Interpolation

YAML supports:

```text
${VAR}
${VAR:-default}
```

Interpolation applies to configuration strings, not to prompt text. Inline
prompt bodies — `runtime.registry.prompts.<name>` string prompts, message
`content`, content-part `text`, and artifact `attach.text` — are never
interpolated: a literal `${NAME}` there reaches the model verbatim and cannot
inject worker environment values into prompts, providers, or traces. Prompt
*config* keys inside a prompt block (`model`, `temperature`,
`provider_params`) interpolate normally.

In configuration strings, escape with a doubled dollar sign: `$${NAME}`
renders a literal `${NAME}` (and `$${NAME:-d}` renders `${NAME:-d}`) without
any environment lookup.

Duplicate mapping keys are load errors across all Typeflux YAML — workflow
specs, project manifests, environment profiles, and policies. Standard YAML
parsing silently keeps the last duplicate, which would let a file display one
value to a reader while the runtime and the resolved workflow bundle use
another. YAML merge keys (`<<`) keep their normal override semantics and are
not treated as duplicates.

YAML documents are bounded at 1 MiB and 1000 alias references. These bounds
keep pathological documents from amplifying through config traversal, but
they are sized for operator-trusted files, not adversarial input:
hosted/multi-tenant deployments must treat tenant-supplied YAML as untrusted
beyond these limits (sandboxed parsing belongs to hosted-mode hardening).

Missing variables without defaults fail during load. Single-workflow YAML
commands load the repo-root `.env` automatically.
`TYPEFLUX_ENV_FILE` can point at another ignored profile, such as
`.env.temporal-cloud`, for live Temporal Cloud smoke tests. Environment
variables are consumed when the YAML interpolates them or when a supported
runtime field uses `value_from.env`. Exporting `TEMPORAL_TLS` or
`TEMPORAL_API_KEY` has no effect unless the YAML contains matching
`runtime.temporal` entries or a matching typed secret reference.

Project environment commands are explicit-profile driven. They load env files
declared by the selected environment profile and suppress implicit cwd `.env`
loading while resolving, building, and running that project workflow.

### Hermetic interpolation for committed artifacts

By default `${VAR}` references resolve against the process environment
(`os.environ`) — the operator's shell is the base the environment's
`variables:`/`.env` overlay layers over. That is the right default for running a
workflow locally, but a consumer that emits **committed, machine-independent
artifacts** (deployment plans/renders whose bytes must not carry the operator's
shell) needs interpolation to resolve against a fixed, declared environment
instead.

For that, `resolve_project_workflow(..., base_env=<mapping>)` (and the lower-level
`load_yaml_spec(..., env=<mapping>)`) take an explicit base environment. When
provided:

- Interpolation is **completely hermetic** — the injected mapping is the only
  source. A `${VAR}` whose name is absent from it errors exactly as an unset
  shell variable would; there is no silent fallback to `os.environ`. An injected
  value of `""` is *set* (an empty string), not missing.
- The environment's `variables:`/`.env` overlay still layers **over** the
  injected base (overlay wins), matching the overlay-over-`os.environ` precedence
  of the default path.
- Resolution runs purely functionally and never mutates `os.environ`, so it is
  safe under concurrency and async — replacing the fragile
  swap-`os.environ`-around-each-call wrapper a consumer would otherwise maintain.
- The base is retained on the resolved artifact, so **sub-workflow closures are
  hermetic too**: sub-workflow resolution, the closure-policy admission walk,
  and the workflow-class build all re-resolve children under the parent's
  injected base — never the shell.
- **Dotenv loading is off** under an injected base: a hermetic call never lets a
  cwd `.env` / `TYPEFLUX_ENV_FILE` mutate `os.environ` as a side effect. An
  explicit `load_dotenv=True` alongside `env` is honored by merging the dotenv
  values into the interpolation map only (injected keys win); the process
  environment still stays untouched.

The deployment-plan writer is wired to this seam:
`write_deployment_plan(..., base_env=<mapping>)` computes the plan's
`spec_digest` (including its sub-workflow closure) against the injected mapping,
and `verify_deployment_plan(..., base_env=<same mapping>)` reproduces it at
promote time on any machine — so a committed plan file is machine-independent.
`build_project_deployment_plan` takes the same `base_env` for the rendered
`deployment-plan.json`/ConfigMap artifacts. Scope note: the bundle's
*validation* checks (policy env references, connection probes) still read the
process environment by design — they are runtime gates, not artifact bytes.

On the CLI (#798), `typeflux-project deploy --base-env-file plan.env` loads the
file's KEY=VALUE pairs as this base (`--hermetic` is the empty-base form; the
two are mutually exclusive), so `--plan-out` plan bytes are reproducible across
machines given the same file:

```sh
typeflux-project deploy typeflux.project.yaml \
    --environment prod --workflow review --image "$IMAGE" \
    --base-env-file plan.env --plan-out deployments
```

`generated_at` is the one per-run field (excluded from `plan_hash`, so the same
composition still yields the same hash and filename).

Omitting `base_env` preserves the default `os.environ`-backed behavior exactly.
