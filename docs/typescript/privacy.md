# Privacy & Data Protection (TypeScript)

The TypeScript counterpart of [Privacy & Data Protection](../privacy.md). The **payload
codec wire format, the redaction contract, and the governance knobs are the same portable
contract** in both SDKs — a payload encrypted by the Python edition decrypts in TypeScript
and vice versa, and the *same YAML* configures both. Read the [Python doc](../privacy.md)
for the full key model, rotation runbook, threat model, governance semantics, and the
retention/erasure design; this page covers only what differs in TypeScript.

## What is identical

- **Payload codec** — AES-256-GCM whole-payload encryption, `type: aes` under
  `runtime.temporal.payload_codec`, the keyed-map rotation model, `value_from` key
  resolution, and the pinned wire format (`binary/encrypted`, `typeflux-key-id`,
  `nonce(12)||ciphertext||tag(16)`). Off unless declared; fail-closed on a missing/unknown
  key. Built on `node:crypto` (the Python edition uses `cryptography.hazmat`) but
  byte-compatible.
- **`required: false` on a codec key is admission-only** — same two-layer semantics as the
  Python edition. A key's `value_from` accepts the optional `required` flag every secret
  reference does, and `required: false` relaxes **only the admission/plan presence check** (the
  rendered Kubernetes Secret ref becomes `optional: true`, so deploy tooling won't flag the key
  as a missing-required secret). It does **not** make the key runtime-optional:
  `buildPayloadCodec` — and the symmetric client-codec check `runWorkflow` performs —
  **always** fail-closes on an unset, empty, or wrong-length (≠ 32 bytes) key at worker start /
  client-connect, regardless of the flag. `required: false` means "don't block the plan on this
  secret," never "run without the key." See the [Python doc](../privacy.md) for the full note.
- **Custom redaction rules** — `observability.redaction.custom_rules: [{name, pattern,
  replacement}]`, appended after the built-in email/SSN/phone/card catalog, with
  `DEFAULT_EXCLUDED_PATHS` still winning over any custom rule.
- **Governance** — `runtime.temporal.require_payload_codec`,
  `observability.redaction.require_custom_rules`, and the `require_payload_codec` risk-tier
  macro, all fail-closed at admission, OR-merging (and union-merging the required rule
  names) across composed policies, and surfaced in the resolved-workflow bundle's
  `risk_tier.requirements`.
- **Subject-id opacity (#715)** — identical contract, identical warning: `subjects:`
  values land in **plaintext** channels no codec or redaction touches (the
  `TypefluxSubjectIds` search attribute — visibility-API/UI readable — and the Langfuse
  `userId` + `typeflux.subject:{id}` tags). A subject id MUST be an opaque pseudonymous
  handle (an internal surrogate key like `subject-0001`), never an MRN, name, email, or
  other raw PHI/PII. See the [Python doc](../privacy.md)'s data-paths table.

## What differs — the regex dialect

Custom-rule `pattern`s are compiled with the **JS `RegExp`** engine in this edition (the
Python edition uses Python's `re`). The *same YAML* is validated by each edition's own
engine at spec load — an invalid pattern fails `loadYamlSpec` fail-closed with a pointer
error. A **portable** rule sticks to features common to both dialects (character classes,
anchors, bounded quantifiers, alternation). Avoid engine-specific constructs — Python-only
inline flags like `(?i)`, `\p{…}` Unicode property escapes with differing support, or
differing named-group syntax — unless you accept per-edition behavior on that rule.

```yaml
runtime:
  observability:
    type: langfuse
    redaction:
      custom_rules:
        - { name: case_reference, pattern: 'CASE-\d{6}', replacement: '[REDACTED_CASE]' }
```

Rules compile with the global flag internally, so every match in a string is replaced
(matching Python's `re.sub` semantics).

## Wiring

The codec is built once per `buildRuntime` and applied to **both** the worker's
`dataConverter` and the client that starts workflows (a start-path payload that skips the
codec would ride plaintext — the runtime fail-closes if the client is un-wired). Redaction
is applied by the `TraceWriter` over your injected `TraceTransport`; there is no TS
trace-reading CLI (point the Python `trace` tooling at the same backend to read
TS-emitted traces). See the runnable
[`privacy-governance` example](../../packages/typescript/temporal-yaml/examples/privacy-governance).

## Retention & Erasure

Identical across editions — see the [Python doc's Retention & Erasure
section](../privacy.md#retention--erasure). The erasure map (Temporal history,
observability traces, the cross-run cache, provider logs, exported bundles), the
per-surface mechanisms below, the `erase` operation, and the note that
`artifacts.max_bytes` is a size ceiling **not** retention all apply unchanged. The
erasure epic (#715) is complete in both editions; the TS orchestration seam and CLI are
described at the end of this section.

### Temporal crypto-shred & history erasure (#715 slice 4)

The Temporal surface's erasure levers ship behaviorally identical to Python (see the
[Python doc](../privacy.md#temporal-crypto-shred--history-erasure-715-slice-4) for the
full mechanics). Per-subject **crypto-shred** is opt-in via
`runtime.temporal.payload_codec.subject_scope: {}`: `buildRuntime` then wraps the codec
in a `SubjectScopedPayloadCodec` over a `SubjectKeystore`
(`subjectId → 32-byte record`), and `destroySubjectKey(subjectId)` is the erasure
primitive (the record's bytes are dropped and a permanent tombstone left, so it is
unrecoverable through the API). The codec learns each payload's owning execution from
the SDK's **serialization context** (passed to `encode`/`decode` on standard
client/worker paths) and resolves `workflowId → subject ids` via the **start-path
registry** (`runWorkflow` registers the same ids it stamps into `TypefluxSubjectIds`,
including the empty set) with a **visibility-describe fallback** — the
`typeflux-yaml-worker` entrypoint binds a visibility client so a worker resolves
executions other processes started; a resolution that cannot be honored **fails
closed**, never a silent shared-key seal. Subject payloads seal under a key **combined
(`SHA-256`) from the subject records** with a `tfsubj1:<subject>` kid (byte-pinned in
`binding.v1.json` — a record shredded by one edition is unreadable in the other); a
**no-subject** execution takes the shared-key path **byte-for-byte** (the pinned shared
vectors run through the wrapper's fallback in the test suite). The default
`InMemorySubjectKeystore` is **process-local** and is minted by default **only** by
`buildRuntime` (the sole-owner path: one process holds the worker and the start-path
converter); the split-process surfaces **fail closed** without an injected shared
backend — the `typeflux-yaml-worker` entrypoint requires worker bindings
`subjectKeystore`, and the TS control plane rejects subject-scoped projects outright
(no keystore seam yet). Production injects a shared backend via
`BuildRuntimeOptions.subjectKeystore`. Shred semantics are
**fail-closed**: a destroyed subject never re-mints (encode throws), and decode of its
history throws the **distinct `SubjectKeyShreddedError`** (a `PayloadCodecError`
subclass) — never plaintext, never a silent empty. A **mixed-subject** execution seals
under all its subjects' combined records, so erasing any one member shreds the shared
history; per-payload shred inside a mixed execution is a documented non-goal
(content-blind codec), and **sub-workflow composition is rejected at build time** under
`subject_scope` (a child's input is encoded before the child exists). The
**`DeleteWorkflowExecution`** complement — `deleteExecutionsForSubject(client, subjectId,
{ dryRun, namespace, limit })` — removes **closed subject-dedicated** executions
outright; exclusions are fail-safe and categorized (`multi_subject` by count — never the
other ids; `unreadable_subjects`; `stale_index` for a readable set that disowns the
target; `unknown_status` — closed-ness is decided against the SDK status-name set,
never a string default), **running executions are reported, never touched**, dry-run
defaults ON, and the report separates the `deletable` plan from the authoritative
`deleted` outcome and carries the index-coverage caveat. HKDF-derived keys were
rejected (no clean single-subject destroy — the master is re-derivable); the
record-combine uses real destroyable records, not a master.

### Langfuse subject-trace deletion (#715 slice 2)

The Langfuse surface's **deletion primitive** ships behaviorally identical to
Python (see the [Python doc](../privacy.md#langfuse-subject-trace-deletion-715-slice-2)
for the full mechanics): `deleteTracesForSubject(client, subjectId, { dryRun, since, until })`
uses the pinned `langfuse` SDK's native trace list + bulk-delete API to remove
every trace attributed to a subject. It queries by BOTH the `typeflux.subject:{id}`
tag and the native `userId` and takes the de-duplicated **union**, walks every
result page (warning explicitly if the page-cap ceiling truncates a scan),
defaults to **dry-run** (returns what it *would* delete without mutating), and
reports ids/counts only — never trace PII — with per-batch failure reporting.
Multi-subject traces are **excluded and reported as `conflicted`** (trace id + a
count of other subject markers, never their ids — that would leak other subjects'
presence into this subject's report); the conflict check unions every readable
tag list across the two channels, and rows no channel could read are excluded as
conflicted-unknown, fail-safe. The subject filter is **fail-closed**: the TS path
passes it on a typed query object verbatim (no argument-stripping exists), and a
client whose `api` lacks the trace endpoints raises the pointed not-supported
error up front. Every report carries the same **index-coverage caveat**: traces
written before subject tagging (pre-slice-1) carry neither carrier and are
invisible to the query channels, so the report is complete for post-slice-1
traces only. A backend without a deletable trace store raises a pointed
not-supported error rather than silently no-op'ing.

### Cache erasure (`SubjectErasableCacheStore`, #715 slice 3)

The cross-run cache surface is real tooling in both editions: a `cacheStore`
implementing the optional **`SubjectErasableCacheStore`** capability
(`eraseSubject`, #715 slice 3; the reference `InMemoryCacheStore` does)
invalidates a subject's memoized outputs from a write-time subject→key index,
with a documented full-flush fallback for plain stores and an
[audit-honesty coverage
caveat](../privacy.md#cache-erasure-subjecterasablecachestore-715-slice-3).

### The `erase` operation (#715 slice 5)

The orchestration seam and CLI ship behaviorally identical to Python (see the
[Python doc](../privacy.md#the-typeflux-erase-operation-715-slice-5) for the full
walkthrough: dry-run → review the receipt → execute). In TypeScript:

- **Library seam** — `eraseSubject(subjectIds, deps, { actor, dryRun, surfaces,
  since, until, executionLimit })` → `ErasureReceipt` (exported from
  `@typeflux/temporal-yaml`), with `erasureFailed(receipt)` as the caller's
  non-zero-exit signal. `deps` injects the surfaces: `{ temporalClient,
  temporalNamespace, subjectKeystore, langfuseClient, cacheStore }` — a selected
  surface with a missing dependency is reported **skipped with a reason**, never
  silently omitted, never a crash.
- **CLI** — the `typeflux-project` bin gains the `erase` verb:

  ```
  typeflux-project erase typeflux.project.yaml \
      --workflow review --environment prod --subject subject-0001 \
      [--surface temporal,langfuse,cache] [--since/--until ISO-8601] \
      [--dry-run | --execute --acknowledge-irreversible] \
      [--actor NAME] [--bindings ./erase-bindings.mjs] [--json]
  ```

  **Dry-run is the default** and performs zero mutation — the keystore is probed with
  the non-minting `subjectKeyState` introspection (an optional `SubjectKeystore`
  method with a read-only `dataKey({ create: false })` fallback: a dry run never
  mints a record and never leaves a tombstone). Executing the **temporal** surface
  requires `--acknowledge-irreversible`. `--bindings` names the same module shape a
  deployed worker uses (`TYPEFLUX_WORKER_BINDINGS` is the fallback), exporting
  `{ subjectKeystore?, cacheStore? }` — the process-local reference backends hold no
  records minted elsewhere, so the CLI never fabricates one. The Temporal client is
  built from the manifest's `runtime.temporal` connection config (TLS/api-key
  honored); it never encodes/decodes payloads (visibility + delete only), so no
  codec/keystore is needed for the executions half. The Langfuse client builds from
  `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (+ `LANGFUSE_HOST`) under the
  environment's variable overlay; missing creds mean an honestly-skipped surface.
- **The receipt is the proof** — `--json` emits the `ErasureReceipt` (camelCase keys;
  ids/counts only, never erased content, never other subjects' ids) for the caller to
  persist **outside the erased surfaces**. Every receipt carries all three surface
  reports (skipped ones with reasons), aggregated warnings, and the always-present
  `unreachable` block (provider logs, exported artifacts, the mixed-workflow
  granularity limit). A failed surface exits non-zero.
- **Control-plane intake is deferred** with the same recorded re-open trigger as
  Python: a hosted-console "erase subject" button (then the #616 contract-first
  flow).
