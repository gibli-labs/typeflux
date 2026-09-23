# Insurance claim review — TS YAML runtime example

The TypeScript port of the canonical Python example
([`packages/python/examples/insurance_claim_review`](../../../../python/examples/insurance_claim_review)):
a claims pipeline that fans out an evidence review over every submitted item
(`map` + `collect`) and consolidates the reviews into a claim-level disposition
packet.

What the spec exercises end to end:

- **Env-interpolated runtime config** (`${TEMPORAL_ADDRESS:-localhost:7233}`, model override).
- **`provider_limits`** (#529) — per-model `max_concurrent` + `min_interval_seconds`,
  one shared limiter across the whole fan-out.
- **Map fan-out** (`over: input.evidence`, `concurrency: 3`) with `collect` into a
  typed batch, threaded into the consolidate step.
- **Zod schema resolution** (`schemas:...` → [schemas.ts](./schemas.ts)) with the
  same field descriptions and refinements as the pydantic originals.

## Python ↔ TS mapping

The Python spec loads `AIActivity` objects via `activities.modules` and resolves
prompts from Langfuse; both are **injected** in TS (documented permanent
divergences), so this port declares the activities in the spec
(`activities.definitions`) and inlines the prompt text
(`registry: {type: inline}` — swap in `type: langfuse` for the managed variant:
with `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` exported it resolves prompts
out of the box over the official SDK, no `registryTransport` needed). Datetimes
ride as ISO-8601 strings — the wire shape either SDK sees.

**What this offline port deliberately omits** (the TS spec supports all of it):

- The Python spec's `observability` block with Langfuse tracing + PII
  **redaction** — TS wires the same blocks via `observerFromSpec`/
  `redactionFromSpec` with an injected transport; an offline example has no
  backend to trace to. The sample data is accordingly ADAPTED, not verbatim:
  the Python original plants deliberate PII (a card number, a phone number) to
  demo that redaction, trimmed here.
- The Python activities' deterministic **normalization hooks** (copying
  claim/evidence ids, sorting risk signals, computing `evidence_count`/
  `approval_required`) — injectable in TS via `defineActivitiesFromSpec`'s
  `hooks` option; the scripted provider returns normalized data already, so
  this port skips them (the consolidate prompt likewise drops the Python
  sentence that references the hook). Against a real provider, add the hooks.
- `structured_mode: json_schema` (TS providers only do json_schema — the field
  is accepted but redundant) and the `tls`/`api_key` connection blocks (this
  example targets a local dev server).

The scripted provider reports `providerName: "openai"` so the spec's
`provider_limits` **model tier actually engages** for the offline fan-out —
provider-limit selection keys off the provider name, and the CI test asserts
the `provider:openai/model:gpt-4o-mini` policy governed every call.

## Run it

Offline apart from the Temporal dev server — the scripted provider
([fakes.ts](./fakes.ts)) mirrors the Python `FakeProvider`, so no API key is
needed. (To run against the real vendor instead, drop the injected `provider`
from `main.ts`: a spec `provider.type: openai` builds over the official SDK
from `OPENAI_API_KEY` out of the box.)

```sh
temporal server start-dev            # in a separate terminal
pnpm install && pnpm -r build        # from the repo root
pnpm --filter @typeflux/temporal-yaml example:insurance
```

The workflow prints the consolidated `ClaimReviewPacket` (recommendation
`investigate`, one duplicate-invoice risk signal, adjuster approval required).

The example is also executed on every CI run — without a Temporal server — by
[`test/example-insurance-claim-review.test.ts`](../../test/example-insurance-claim-review.test.ts),
which assembles this exact YAML + schemas via `assembleYamlRuntime` and invokes
both activities directly.
