# Multimodal Content Parts

> Using the TypeScript SDK? See [Multimodal Content Parts (TypeScript)](typescript/content-parts.md).

Typeflux prompt messages are provider-portable. A `ChatMessage.content` is either
a plain `str` or a tuple of **content parts**, and each provider adapter
translates those parts into its native request shape. Registry code, workflow
code, and manifests never deal in provider-specific message schemas.

## The content-part model

`core/artifacts.py` defines the canonical parts; `ChatMessage.content` is
`str | tuple[ContentPart, ...]` (`core/contracts.py`).

| Part | Purpose | Portable? |
| --- | --- | --- |
| `TextPart(text)` | literal or templated text | yes |
| `ArtifactPart(artifact, text=None)` | reference one named artifact (+ optional lead text) | yes |
| `ArtifactGroupPart(group, text=None)` | reference a named artifact group | yes |
| `ProviderExtensionPart(provider, payload)` | escape hatch: a raw provider-native block, only applied when `provider` matches the active provider | provider-specific |

A bare string is normalized to a single `TextPart` (`normalize_content_parts`),
so text-only prompts stay simple.

The parts reference artifacts **by name**, not by value — binaries are resolved
at execution time from the activity's `artifact_inputs`, never embedded in the
prompt, registry, or manifest.

## Two ways to send multimodal content

1. **Activity-attached (the common path).** The activity declares
   `artifact_inputs` with an `attach` rule; the runtime appends an artifact
   message at execution time. The prompt registry only supplies text. This is
   how [contract_risk_review](../packages/python/examples/contract_risk_review) sends a PDF —
   see `attach: { role: user, text: "Contract PDFs:" }`.
2. **Inline parts.** A prompt (inline or a custom registry) sets
   `content` to a tuple containing `ArtifactGroupPart`/`ArtifactPart`. Useful
   when the prompt itself interleaves text and references.

> Registry-stored templates (Langfuse/LangSmith) are text-only: media is **not**
> embedded *inside* a stored template. This is a deliberate limitation, not a
> gap (#335) — the canonical way to send media with any registry is the attach
> path above (`artifact_inputs` + `attach`), which keeps binaries out of the
> registry, prompt, and manifest. For media interleaved with prompt text, use an
> inline or custom registry whose prompt sets `ArtifactGroupPart`/`ArtifactPart`.

## Stable artifacts and the session cache (`cache: reference`)

An `artifact_input` may set `cache: reference` (Python `cache_role="reference"`,
TS `cache_role: "reference"`) to mark its document as part of the **stable prefix**
shared by every item of a `map` fan-out — a policy PDF, a rubric, a long contract
that never varies item to item. How that stable artifact is cached depends on the
provider's session-cache **style** (#60/#363/#362):

- **Reference style** (Gemini) — the artifact is resolved and uploaded **once** at
  prep time into a server-side cache object (`cachedContent`); per-item calls
  reference it by id and do **not** re-send the bytes.
- **Prefix style** (Anthropic, OpenAI) — there is no cache object. The per-item
  message assembly instead **lifts the reference artifact to the front of the
  conversation**, right after the system prefix and *before* the varying per-item
  turn: `[system…, reference attach…, per-item turn…, per-item artifact attach…]`.
  The document is re-sent every call but billed at cache-read rates after the
  first item (Anthropic marks the `cache_control` breakpoint on the last stable
  reference turn; OpenAI caches the byte-prefix implicitly). Uncached runs and
  reference-style runs keep the plain **append-last** order unchanged.

**Identical-across-items contract.** A `cache: reference` artifact must be
byte-identical across all items of the map step. Two halves, enforced differently:

- **Divergent presence is unrepresentable.** An optional (`required: false`)
  reference artifact must give its `attach` a static `text` — the attach message
  is then emitted for every item whether or not the artifact resolves, so the
  conversation shape (and the prefix breakpoint index) never varies. A textless
  reference must be `required`. The invalid optional-textless shape is rejected
  at construction and at YAML load (fail-closed).
- **Divergent bytes are not runtime-verified.** They do not corrupt anything —
  they simply miss the cache (billing waste), the same documented contract as
  the Gemini reference case.

**Anthropic system-text constraint.** Anthropic's system prompt is text-only. A
reference artifact whose `attach.role` is `system` may carry **text** (it folds
into the cached system block), but a *document* placed in a system message has no
text to fold and **raises** a `ProviderConfigError` — document references must
attach as `user`/`assistant` turns to ride in the cached prefix. Accordingly, only
reference inputs that attach as non-system turns count toward the prefix breakpoint.

**Prefix-style conversation breakpoint by shape (Anthropic, #362/#698).** The
system block is always cached when a session is engaged. Where the *conversation*
breakpoint lands depends on the shape (the executor tells the provider via two
content-free handle fields — `prefix_stable_messages`, a count, and
`per_item_artifact_messages`, a boolean — because the provider cannot tell a stable
turn from a varying one positionally):

| Conversation shape | Signal | Conversation breakpoint |
| --- | --- | --- |
| `[stable instructions turn, per-item turn]` (no artifacts, the #60 shape) | neither field set | last stable turn (`conversation[-2]`) |
| `[reference attach…, per-item turn(s)]` | `prefix_stable_messages = k > 0` | last reference turn (`conversation[k-1]`) |
| `[per-item query, per-item artifact attach]` (no reference input, #698) | `per_item_artifact_messages = true` | **none** — the last two turns both vary, so there is no stable conversation span; only the system block caches |

The last row is the fix for #698: marking `conversation[-2]` there would land on the
varying per-item query, keying the conversation cache on content that differs every
item (a silent cache miss). When a reference span *is* present it stays authoritative
(`prefix_stable_messages` wins; the per-item flag is ignored). **OpenAI** needs no
adapter change — its cache is the implicit byte-prefix, so the varying query + per-item
artifact shape simply never conversation-caches, while the stable system prefix still can.

## Provider compatibility matrix

`supported_artifact_kinds` is checked at preflight (fail-loud, before the
workflow runs); the provider boundary then applies finer media-type/source
rules. Source kinds: `local_path`, `url`, `provider_file`.

| Kind / source | openai | anthropic | gemini |
| --- | --- | --- | --- |
| image (local → inline/base64) | ✅ | ✅ (gif/jpeg/png/webp) | ✅ |
| image (url) | ✅ | ✅ | ✅ (file_data) |
| document/PDF (local) | ✅ | ✅ | ✅ (inline) |
| text data (local, read as text) | ✅ | ✅ | ❌ |
| provider_file (file_id) | ✅ | ✅ | ❌ (Files API → #358) |
| audio / video (local → inline) | ❌ | ❌ | ✅ (inline) |
| archive | ❌ | ❌ | ❌ |

`ProviderExtensionPart` lets a prompt carry a raw provider-native block for one
provider; adapters drop blocks addressed to a different provider.

Gemini also spends **thinking tokens** against `max_output_tokens` — budget
`provider_params.max_tokens` for thinking *plus* output (see
[provider-portability.md](provider-portability.md)).

## Rendering, hashing, and safety

- **Rendering** — mustache `{{var}}` interpolation applies to `TextPart` text and
  the optional `text` on artifact parts; artifact references and provider
  extensions pass through untouched (`render_content_parts`). The grammar is
  dotted identifiers only — safe, literal substitution.
- **Hashing** — `messages_hash` hashes `content_part_payload`, which carries the
  logical artifact *name* and text, never raw bytes.
- **Manifests** — artifacts appear via `ResolvedArtifact.safe_summary()`:
  `source_kind`, `kind`, `media_type`, `role`, `sha256`, `size_bytes` — **no raw
  path, URL, or content**. Provenance and integrity without leaking the payload.

## Related

- [Provider Portability](provider-portability.md) — the no-silent-divergence
  contract and the capability matrix.
- [Extending Typeflux Temporal](extending.md) — the `ModelProvider` contract,
  `supported_provider_params`, and `supported_artifact_kinds`.
- [YAML Runtime](yaml.md) — declaring `artifact_inputs` and `attach`.
