# Observability (TypeScript)

The TypeScript counterpart of [Observability](../observability.md). The **trace
shape, execution manifests, tags-vs-metadata split, and redaction rules are the
same portable contract** in both SDKs — what differs is the egress path: the TS
runtime writes observations to an **injected trace transport**, and there is no
TypeScript trace-reading CLI (the `trace list/search/inspect/export/diff` tooling
remains Python-only; point it at the same backend to read TS-emitted traces).

Read the [Python doc](../observability.md) for the full trace-shape, manifest,
and CLI reference; this page covers the TS write surface and the redaction
contract.

## The write path

The TS observability core lives in
[`@typeflux/temporal`](../../packages/typescript/temporal):

```text
ActivityObserver   – receives activity/generation/hook observations
TraceWriter        – redacts, then emits to an injected TraceTransport
TraceTransport     – your adapter to Langfuse / OTEL / a sink (structural)
CollectingObserver – an in-memory observer for tests
```

Providers report `ProviderUsage` (including `cacheReadTokens` / `cacheWriteTokens`)
through a per-call `usageSink`; the executor sets those as usage details on the
generation observation — so usage lands the same way under any backend and any
provider (the portable contract, Python #340). Typeflux performs no cost math
itself; the model and token counts are operational metadata only.

Trace observations **do** carry content: a generation observation includes the
rendered prompt messages (its input) and the provider result (its output). That
content is what the [redaction](#redaction) boundary masks before it reaches the
transport — so treat the trace transport as an egress point and configure
redaction (or your own transport-side handling) accordingly. Only the
high-cardinality identifiers and hashes are metadata-only (see
[tags vs metadata](#tags-vs-metadata)).

For the YAML runtime, wire the observer from the spec:

```ts
import { observerFromSpec, buildRuntime } from "@typeflux/temporal-yaml";

// runtime.observability.type must be a tracing backend (e.g. langfuse) or the
// observer is undefined (type: none / unset → no observer, no egress).
const observer = observerFromSpec(spec, transport); // `transport` is your injected TraceTransport
const runtime = await buildRuntime(spec, { provider, schemas, observer });
// …drain on shutdown (the observer buffers, then flushes each trace to the
// transport's submitTrace — Python flushes on worker shutdown):
await observer?.flush();
```

Like Python, YAML tracing is **explicit opt-in**: `runtime.observability.type`
must name a backend before any observer is constructed. Ambient backend
credentials do not by themselves change trace-egress behavior.

**Langfuse and LangSmith work out of the box** (Python parity): when the spec
declares `observability: {type: langfuse}` or `{type: langsmith}`,
`buildRuntime` wires the observer itself over the OFFICIAL SDK — no transport
code in your app. Each SDK is an optional peer dependency loaded lazily (the
TS mirror of Python's `[langfuse]`/`[langsmith]` extras): install the one your
spec names (`npm/pnpm add langfuse` or `add langsmith`), export its standard
credentials — `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (plus
`LANGFUSE_HOST` for self-hosted), or `LANGSMITH_API_KEY` (plus
`LANGSMITH_PROJECT`, default "default", and `LANGSMITH_ENDPOINT`) — and every
run's traces land in your project. Missing keys degrade to an untraced run
with one warning; a spec that opts in without its SDK installed fails loudly
with an install hint. Traces STREAM per completed activity; call
`runtime.drainObservability()` before a short-lived process exits so the tail
ships. An explicit `observer` option always wins — that is how custom
backends (and tests) inject their own transport via `observerFromSpec`.

## Execution manifests

The manifest content is the same reproducibility contract as Python and its
hashing is conformance-tested against the Python goldens for aligned shapes:

- workflow identity + Temporal IDs, workflow contract hash and resolved manifest
  hash, code provenance, spec identity
- per-activity: schema names + hashes, prompt refs + resolved versions, template
  and rendered-message hashes, provider settings, hook identity, validation
  attempt, definition source (`yaml` / `code`)
- policy identity + admission status when project policy is applied

Two workflow-level hashes carry the same meaning as Python:
`workflow_contract_hash` answers "did the logical contract change?" (name, spec
identity, map-step shape, activity rollups); `manifest_hash` answers "did the full
resolved execution context change?" (the contract hash plus runtime/provenance
fields). The same contract in a different namespace/queue/region keeps its
contract hash and gets a new manifest hash; a prompt/schema/step change moves
both.

## Tags vs metadata

Tags locate traces; metadata reconstructs them — the same split as Python. Low-
cardinality `typeflux.*` tags (workflow/activity/prompt/model/env, lifecycle)
locate; high-cardinality fields (workflow/run/activity IDs, hashes, prompt
versions, policy hashes) are metadata-only. Secret references, TLS material, raw
prompt/response content, and raw artifact bytes are never recorded under
`typeflux.*`.

## Redaction

Redaction runs **before** any observation reaches the transport, so a
user-supplied transport gets the same protection as a Typeflux-built one. The
default rules mask emails, US phone-like numbers, SSN-like values, and
credit-card-like digit runs while preserving Typeflux/Temporal operational
metadata so search, diff, and reconstruction keep working.

Map `runtime.observability.redaction` from the spec with `redactionFromSpec(spec)`,
or pass a `RedactionConfig` directly to the `TraceWriter`. Error status messages
are sanitized everywhere: provider and prompt-resolution errors keep their
already-sanitized messages, validation failures reduce to an error count + schema
name, and any other exception is reported as its type name only — raw exception
text never bypasses the boundary.

This is observability redaction only. Typeflux does not redact prompts before
model invocation unless you add explicit application logic.

## Reading traces

There is no TS trace-reading CLI. TS-emitted traces are read with the Python
observability CLI pointed at the same backend, or with your backend's native UI
(the tags and manifest metadata are identical). See the
[Python CLI reference](../observability.md#cli) for `trace list/search/inspect/
export/diff`.

## See also

- [Concepts](concepts.md) — manifests as the reproducibility contract
- [YAML Runtime](yaml.md) — declaring `runtime.observability`
- [Observability (Python)](../observability.md) — full trace-shape + CLI reference
