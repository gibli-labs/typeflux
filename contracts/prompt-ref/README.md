# Prompt-ref contract (#390)

The reference a Typeflux AI activity uses to resolve a prompt from a registry.
Frozen as part of the cross-SDK contract bundle (see `../CONTRACT_VERSION`).

Golden: [`golden/prompt_ref.json`](golden/prompt_ref.json) (generated from the
Python baseline, `PromptRef.to_dict`).

| Field | Type | Notes |
|---|---|---|
| `name` | string | required |
| `version` | integer \| null | immutable pinned version |
| `label` | string \| null | mutable label (e.g. `production`, `canary`) |

`version` and `label` are mutually exclusive (pin a version *or* track a label).

**`prompt_type`** (`"auto"` \| `"text"` \| `"chat"`) is a *resolve-time hint* on
the in-memory `PromptRef` — it tells the registry how to interpret the prompt
(text vs chat). It is **not part of the serialized contract**: `to_dict` emits
only `name`/`version`/`label`. The type is applied at resolution from the
activity definition, so each SDK reads it from its own definition and it never
needs to round-trip on the wire.
