# Multimodal Content Parts (TypeScript)

> Using the Python SDK? See [Multimodal Content Parts](../content-parts.md) —
> the fuller reference for the part model, artifact policy, and manifest
> provenance. This page carries the TypeScript surface.

Typeflux prompt messages are provider-portable. A message's content is either a
plain string or a sequence of **content parts**, and each provider adapter
translates those parts into its native request shape. Registry code, workflow
code, and manifests never deal in provider-specific message schemas.

## The part model

`ChatContent` is `string | ContentPart[]`, and `ContentPart` is a discriminated
union of four kinds — the same four the Python SDK defines:

```ts
import type { ChatContent, ContentPart } from "@typeflux/temporal";

type TextPart              = { type: "text";               text: string };
type ArtifactPart          = { type: "artifact";           artifact: string; text?: string };
type ArtifactGroupPart     = { type: "artifact_group";     group: string;    text?: string };
type ProviderExtensionPart = { type: "provider_extension"; provider: string; payload: Record<string, unknown> };
```

- **`text`** — literal prompt text.
- **`artifact`** — a reference to one resolved artifact (a file, an image, a PDF).
- **`artifact_group`** — a reference to a *group* of artifacts routed from
  workflow input, so a single part expands to N attachments at render time.
- **`provider_extension`** — an escape hatch for a capability only one vendor
  has. It is tagged with the `provider` it targets and passes through untouched.

## Two functions

```ts
import { contentPartPayload, renderContentParts } from "@typeflux/temporal";
```

**`contentPartPayload(content)`** serializes content for the cross-SDK message
hash. The invariant that matters: `contentPartPayload(someString) === someString`,
so widening a message from string content to parts leaves string-content hashing
**byte-identical**. An existing prompt's cache identity and manifest hash do not
move because the type widened.

**`renderContentParts(content, renderText)`** applies template rendering to text
only. In a parts list it renders the `text` of text / artifact / artifact-group
parts and passes every other part through unchanged — a `provider_extension`
payload is never template-rendered, which keeps vendor payloads from being
mangled by a stray `${...}`.

## Where artifacts get resolved

The part model is deliberately thin — it is the piece that touches
`ChatMessage`. The heavier subsystem (artifact **sources**, kinds, media-type
policy, sha256 provenance) lives in the worker, not in the core package:

```ts
import { artifactInputResolver, resolveArtifactInputs } from "@typeflux/temporal-worker";
```

That split is why `@typeflux/temporal` stays dependency-light: authoring and
hashing content parts needs no filesystem or network access.

## Declaring artifacts in YAML

The `typeflux.yaml` surface is language-neutral — `runtime.artifacts` with
`local_roots` and `allowed_media_types`, plus `artifact_group` prompt parts
routed from workflow input — and it means the same thing in both editions. See
[YAML Runtime (TypeScript)](yaml.md) for the TS wiring, and
[Content Parts (Python)](../content-parts.md#two-ways-to-send-multimodal-content)
for the field-by-field reference including media-type policy and safe manifest
provenance.

## Provider support is not uniform

An artifact kind one provider accepts may be unsupported by another. Typeflux
never silently drops the part — but *where* the failure surfaces depends on what
the spec declares. An **explicitly declared** unsupported kind fails
**preflight**, before a worker polls. When the kind is inferred from the
resolved artifact, or the incompatibility turns on media type, source kind, or
provider-file ownership, preflight passes and the **provider call fails at
execution** instead. Treat preflight as an early filter, not a total guarantee.
See [Provider Portability (TypeScript)](provider-portability.md).

## See also

- [Concepts (TypeScript)](concepts.md) — where rendering sits in execution order
- [YAML Runtime (TypeScript)](yaml.md) — declaring artifacts and groups
- [Provider Portability (TypeScript)](provider-portability.md) — capability matrix
- [Editions](../editions.md) — parity table
