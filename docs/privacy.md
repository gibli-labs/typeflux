# Privacy & Data Protection

Enterprise privacy controls for Typeflux workflows (#188): **encryption of Temporal
payloads at rest**, **custom PII redaction** on the observability egress, the
**governance knobs** that let a project *require* both, and the **retention & erasure**
design. Both SDKs implement the same portable YAML contract; this page is the Python
reference — the [TypeScript mirror](typescript/privacy.md) covers the edition-specific
regex-dialect note.

Three independent data paths carry subject data, and each has its own control:

| Path | What rides it | Control |
| --- | --- | --- |
| **Temporal history** (workflow/activity IO, review notes, inlined artifact bytes) | the real payloads | **payload codec** — AES-256-GCM whole-payload encryption |
| **Observability egress** (Langfuse/LangSmith traces) | trace metadata + generation IO | **redaction** — built-in catalog + custom rules |
| **Identity/index fields** (the `TypefluxSubjectIds` search attribute; Langfuse `userId` + `typeflux.subject:{id}` tags; OTel `enduser.id`) | the **subject ids themselves**, in **plaintext** | **none at runtime** — the ids MUST be opaque pseudonymous handles (see below) |

The first two are deliberately separate: the codec encrypts *everything* on Temporal
history, so it needs no exclude-list; redaction masks PII on the trace path, which the
codec never touches. A workflow that needs both turns both on (see
[`examples/privacy_governance`](../packages/python/examples/privacy_governance)).

**The third path has NO technical control — the value itself is the control.** Subject
ids (#715) exist to *index* executions, traces, and cache records for erasure, so they
are stored where queries can see them: Temporal **search attributes** are plaintext and
readable through the visibility API and the Temporal UI (the payload codec never touches
them), and the observer identity fields (`userId`, subject tags, `enduser.id`) are set
*after* redaction as first-class fields, so no redaction rule rewrites them. A subject id
must therefore be an **opaque pseudonymous handle** — an internal surrogate key such as
`subject-0001` — never an MRN, a name, an email address, or any other raw PHI/PII.
Opacity is not machine-checkable; this documented contract is the control.

## Payload codec — encryption at rest

Temporal stores every workflow/activity input and output in its event history. Without a
codec those payloads are plaintext JSON. The payload codec (#188 slice 1, merged in
[`1d9c763`](https://github.com/) / PR #714) encrypts the **entire** payload with
AES-256-GCM before it leaves the process, following Temporal's reference-codec pattern.

```yaml
runtime:
  temporal:
    payload_codec:
      type: aes
      current: 2026-q3          # the key id NEW payloads are encrypted under
      keys:
        - id: 2026-q3
          value_from: { env: TYPEFLUX_PAYLOAD_KEY_2026_Q3 }
        - id: 2026-q2           # kept so payloads written under it still decrypt
          value_from: { env: TYPEFLUX_PAYLOAD_KEY_2026_Q2 }
```

- **Off unless declared.** Absent `payload_codec` means plaintext. This is explicit
  enablement, never provider-style auto-wire — a PII codec must not silently appear or
  silently degrade.
- **Wire format (pinned, identical in both SDKs).** The encrypted `Payload` carries
  `metadata["encoding"] = "binary/encrypted"`, `metadata["typeflux-key-id"] = <kid>`, and
  `data = nonce(12B) || ciphertext || tag(16B)`. Because both editions implement this
  byte layout, a payload encrypted by Python decrypts in TypeScript and vice versa.
- **Key material.** Each key is 32 raw bytes, resolved through the standard
  `value_from: {env|file}` secret seam — the same discipline as `api_key`. Every key slot
  joins `SECRET_SLOT_PATHS`, so bundles/plans show the key by `source_kind`/`source_name`
  only; the key **value** never crosses the resolver wire.
- **Fail-closed.** Once declared, every referenced key MUST resolve at client-connect
  time or startup fails. A payload marked `binary/encrypted` whose `typeflux-key-id` is
  unknown raises a decode error — never a silent passthrough of ciphertext or plaintext.
- **`required: false` on a key is admission-only — the runtime always fail-closes.** A codec
  key's `value_from` accepts the same optional `required` flag as any secret reference, but it
  means something narrower than the name suggests. `required: false` relaxes **only the
  admission/plan presence check**: the rendered Kubernetes Secret ref is marked
  `optional: true`, so deploy tooling will not flag the key's env/file source as a
  missing-required secret. It does **not** make the key runtime-optional. `build_payload_codec`
  — and the symmetric client-side codec check `run_workflow` performs — **always** fail-closes
  on an unset, empty, or wrong-length (≠ 32 bytes) key at worker start / client-connect,
  regardless of the flag. So `required: false` means "do not block the plan on this secret,"
  never "run without the key": a declared codec whose key does not resolve refuses to start,
  and never degrades to plaintext. (Both editions behave identically here — TS's
  `buildPayloadCodec`/`runWorkflow` fail-close the same way.)

### Key model & rotation runbook

Keys are a keyed map, not a single value, so rotation is first-class from v1. Encryption
always uses `current`; decryption reads each payload's `typeflux-key-id` and looks up any
known key. To rotate:

1. **Add** the new key to `keys` (resolve its 32 bytes into a fresh env/file secret).
   Deploy. The worker can now *decrypt* under the new kid, but still encrypts under the
   old `current`.
2. **Flip** `current` to the new key id. Deploy. New payloads are encrypted under the new
   key; old payloads still decrypt under their original kid, which remains in `keys`.
3. **Retire** the old key only after every payload encrypted under it has aged out —
   either re-encrypted (a replay/migrate that rewrites history) or expired via the
   namespace's history retention (TTL). Removing a key while live history still references
   it makes those payloads undecryptable (fail-closed), so retire last.

Generate a 32-byte AES-256 key and store it in your secret manager. The codec takes the
resolved secret **verbatim** — it does NOT base64-decode — so the resolved byte length must
be exactly 32. Two honest paths:

* **File source (recommended for production).** `value_from.file` reads the file's *raw
  bytes*, so 32 random bytes give the full 256-bit key:

  ```sh
  openssl rand -out payload.key 32
  ```

  Reference it with `value_from: { file: /path/to/payload.key }`.
* **Env-var source.** `value_from.env` reads the variable as UTF-8 text (trimmed), so it
  must be a 32-*character* high-entropy ASCII string — a 44-char `openssl rand -base64 32`
  is 44 bytes and fails startup. Base64 characters carry ~6 bits each, so 32 of them give
  ~192 bits of effective entropy (ample, but short of the file source's full 256):

  ```sh
  openssl rand -base64 48 | head -c 32
  ```

### Threat model

The codec's threat model is a **read-only history observer** — an operator, backup, or
support engineer who can read Temporal event history but cannot rewrite it. Against that
adversary AES-256-GCM gives confidentiality (ciphertext only) and integrity (the GCM tag
fails authentication on any tamper).

**Accepted downgrade surface (explicit sign-off, D188-3).** A payload *without* the
`binary/encrypted` marker passes decode untouched — this is required for pre-codec and
mixed-history interop (a namespace that enabled the codec mid-life still holds older
plaintext payloads). This marker-strip is a downgrade primitive **only against a
write-capable adversary** (someone who can rewrite history or MITM the gRPC stream to
strip the marker), which the read-only-observer threat model excludes.

**F2 (AAD hardening) — deferred.** Binding the key id and encoding into the GCM
Additional Authenticated Data would make marker-strip detectable, but it would also break
the pinned cross-edition wire vector (`contracts/temporal-binding/payload-codec-vectors.json`)
for a threat the design excludes. It is deferred, not rejected. **Re-open triggers:** the
threat model expands to write-capable adversaries, or an adopter requires marker-strip
protection.

## Custom redaction rules

Redaction runs before trace metadata reaches an observability backend. The built-in
catalog masks four PII classes — email, US SSN, phone, and Luhn-validated card numbers
(see [Observability](observability.md#redaction)). #188 slice 2 lets a spec **append**
jurisdiction- or domain-specific rules:

```yaml
runtime:
  observability:
    type: langfuse
    redaction:
      enabled: true
      preserve_typeflux_metadata: true
      custom_rules:
        - name: case_reference
          pattern: 'CASE-\d{6}'
          replacement: '[REDACTED_CASE]'
        - name: uk_nino
          pattern: '[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]'
          replacement: '[REDACTED_NINO]'
```

- **Order.** Custom rules run *after* the four built-ins, over the already-masked text —
  each rule sees the previous rule's output.
- **Validated at load, fail-closed.** Every `pattern` is compiled with Python's `re` when
  the spec loads; an invalid regex is a startup error, never a silent no-op. Rule `name`s
  must be unique within a spec.
- **Regex dialect is per-edition.** `pattern` is the **Python `re`** dialect here and the
  **JS `RegExp`** dialect in the TypeScript edition — the *same YAML* is validated by each
  edition's own engine. A portable rule sticks to features common to both (character
  classes, anchors, bounded quantifiers); avoid engine-specific constructs (named-group
  syntax, inline flags, `\p{…}` Unicode properties) unless you accept per-edition behavior.
- **Governance metadata is protected.** `DEFAULT_EXCLUDED_PATHS` (the `typeflux.*` /
  `temporal.*` allowlist that preserves manifest hashes, lifecycle state, moderation
  verdicts, etc.) still wins over a custom rule — exclusion is checked per node *before*
  any rule runs, so even a `.+`-greedy custom rule cannot touch governance evidence.

The heavier alternative — replacing the whole redaction backend via `observability.type:
custom` and an injected `Redactor` — remains available when a rule catalog isn't enough.

## Governance — requiring the controls

A project policy can *require* these controls, fail-closed at admission. Both knobs follow
the existing `require_*` pattern: they OR-merge across composed policies (org → tenant →
environment), and a `human_gated`/`prohibited` risk tier can imply them.

```yaml
# a TypefluxProjectPolicySpec
observability:
  redaction:
    required: true
    require_custom_rules:            # named rules the workflow MUST declare
      - case_reference
      - uk_nino
runtime:
  temporal:
    require_payload_codec: true      # reject a workflow with no payload_codec
risk_tiers:
  human_gated:                       # a tier implies the codec via the SAME macro
    require_payload_codec: true      # mechanism as require_redaction (no new engine)
```

- **`require_payload_codec`** — a workflow whose `runtime.temporal.payload_codec` is absent
  is rejected at admission with a pointer error naming the missing block. It sits beside
  `require_tls`/`require_api_key` and OR-merges the same way.
- **`redaction.require_custom_rules`** — a list of rule *names* the workflow's redaction
  config must contain. A missing name fails admission with the missing names listed.
  Because it is a plain list (not an allow-list), composed policies **union** the required
  names — org requires `case_reference`, tenant adds `uk_nino`, the workflow must declare
  both.
- **Risk-tier implication** — adding `require_payload_codec: true` under a tier
  (`human_gated`, `prohibited`) makes that tier expand to the codec requirement, exactly as
  `require_redaction` does. A tier is a named macro over existing controls; there is no
  parallel enforcement engine.
- **Bundle surfacing** — because the tier requirement flows through the shared risk-tier
  evaluation, the resolved-workflow bundle's `risk_tier.requirements` shows
  `require_payload_codec` with its satisfied/unsatisfied state, so a console reads exactly
  what admission enforces.

Both gates run everywhere policy is enforced: spec-load (`build_project_policy_runtime_guard`),
control-plane admission (`admit_spec` / `enforce_policy_selection`), and the sub-workflow
closure cascade — all through the one `validate_project_policy` path.

## Retention & Erasure

> **Status: shipped (#715).** The erasure epic is complete: subject identity + the
> `TypefluxSubjectIds` index (slice 1), Langfuse trace deletion (slice 2), cache
> invalidation (slice 3), the Temporal crypto-shred + `DeleteWorkflowExecution`
> drivers (slice 4), and the **`typeflux erase` CLI + `erase_subject` library seam +
> `ErasureReceipt`** that orchestrate them (slice 5, below).

A data subject's information spans several surfaces, each with a different owner and a
different erasure mechanism. This section defines where the data lives, the per-surface
mechanisms, and the one operation that fans across them.

| Surface | Erasure owner | Mechanism |
| --- | --- | --- |
| **Temporal history** | Typeflux runtime + namespace admin | per-subject **crypto-shred** — destroy the subject's keystore key record and its history is permanently unreadable ciphertext (#715 slice 4) — complemented by **`DeleteWorkflowExecution`** for closed subject-dedicated executions, with namespace **retention TTL** (history ages out) as the admin-owned backstop. |
| **Langfuse / observability traces** | Typeflux egress + backend | **redaction** keeps PII out of traces at write time (the covered egress); already-written traces are removed via the backend's **trace-deletion API**. |
| **Cross-run cache (#753)** | the `CacheStore` | a store that implements the optional **`SubjectErasableCacheStore`** capability (`erase_subject` / `eraseSubject`, #715 slice 3) invalidates a subject's memoized outputs from a write-time subject→key index; a plain store falls back to a full flush (see below). |
| **Provider logs** | the model provider | provider-owned retention/deletion. Document your provider's policy; an in-VPC / non-logging provider path avoids the surface entirely. |
| **Exported manifests / audit bundles** | the caller | caller-owned. Once a manifest or bundle is exported out of Typeflux, its lifecycle is the exporter's responsibility. |

### Ship-tooling vs. runbook — resolved

The #188-era decision deferred both options until a consumer existed. The
a private adopter trigger resolved it: **tooling shipped** (#715). The
surfaces Typeflux controls are erased by the `typeflux erase` operation below; the
provider-log and export surfaces remain document-only (their owners erase them), and
the receipt names them on every run so the boundary is never implicit.

### Temporal crypto-shred & history erasure (#715 slice 4)

Temporal history is the surface that actually holds the payloads. It has two erasure
levers, chosen by execution shape:

- **Per-subject crypto-shred (opt-in, primary).** The shared-key payload codec is
  content-blind, so crypto-shred under it is all-or-nothing per key id — destroying it
  would make *every* subject's history unreadable. Declaring
  **`runtime.temporal.payload_codec.subject_scope: {}`** turns on subject-scoped
  sealing: a **`SubjectKeystore`** maps `subject_id → 32-byte key record` (minted on
  first use), and the runtime wraps the codec so a subject-scoped execution seals under
  a key **derived from its subjects' key records**. **Erasure = destroy the key record**
  (`destroy_subject_key` / `destroySubjectKey`): the ciphertext persists in history and
  backups but is permanently unreadable — workflow-id-independent, no history rewrite
  needed. `typeflux-key-id` carries a `tfsubj1:<subject>` binding so decode routes to
  the right record; the wire scheme (kid format + the SHA-256 combine) is **byte-pinned
  in `binding.v1.json`** so a record shredded by one edition is unreadable in the other.
  Conformance vectors cover no-subject-unchanged (the pinned shared vectors run through
  the wrapper's fallback), subject-scoped seal/unseal, and decode-after-shred.
- **How the codec knows the execution's subjects (the wired seam).** The Temporal SDKs'
  **serialization contexts** hand the codec the owning execution's workflow id on every
  standard client and worker path (Python `WithSerializationContext.with_context`; TS
  passes the context to `encode`/`decode`). The codec then resolves
  `workflow_id → subject ids` through two channels: the **start-path registry** (the
  runtime registers the same ids it stamps into `TypefluxSubjectIds` — including an
  explicit "no subjects" — before starting) and the **visibility fallback** (a worker or
  CP process that did not start the execution *describes* it and reads the
  `TypefluxSubjectIds` search attribute — the erasure index itself; cached). Anything
  else **fails closed**: an encode with no context, or a binding that cannot be
  resolved, raises rather than silently sealing under the shared key. An execution with
  **no subjects** takes the shared-key path **byte-for-byte** unchanged.
- **Keystore backends (deployment boundary, fail-closed).** The reference
  `InMemorySubjectKeystore` is **process-local** and is built by default **only** on the
  sole-owner runtime path (`build_runtime` / `buildRuntime`, where ONE process holds both
  the starting client and the worker, so every encode and decode shares the same
  keystore). **Everywhere else `subject_scope` with no injected keystore is a loud
  build/connect-time error** — the Python control-plane connects (`_connect_client`
  callers), the TS-binding lifecycle driver, the TS worker entrypoint
  (`typeflux-yaml-worker`), and the TS control plane all refuse rather than silently
  minting keys no worker holds. Split starter/worker deployments MUST inject a
  **shared** keystore backend — `build_runtime(subject_keystore=...)` in Python,
  `BuildRuntimeOptions.subjectKeystore` / worker bindings `subjectKeystore` in
  TypeScript — e.g. KMS/Vault/Postgres-backed. The same applies to a control plane that
  must *decode* a subject execution's history or *seal* lifecycle signals to it.
- **Fail-closed shred semantics.** A destroyed subject NEVER re-mints a key: a
  post-erasure encode for it **fails closed**, and decode of its history raises a
  **distinct `SubjectKeyShreddedError`** that names the shred (a `PayloadCodecError`
  subclass in both editions, so broad fail-closed handlers still catch it) — never a
  plaintext passthrough, never a silent empty. Subject ids must therefore not be
  recycled after erasure (the tombstone is permanent).
- **Mixed-subject executions.** An execution that processes several subjects seals every
  payload under a key combined (`SHA-256`) from **all** its subjects' records, so erasing
  **any one** member subject renders the shared history unreadable (the compliant
  AND-semantics). **Per-payload shred *within* a mixed execution is a documented
  non-goal** — the content-blind whole-`Payload` codec cannot see which sub-field belongs
  to which subject, and a content-aware codec would break the whole-`Payload` reference
  invariant pinned in `binding.v1.json`.
- **Sub-workflow composition is not yet supported under `subject_scope`.** A child's
  start payloads are encoded (with the *child's* serialization context) before the child
  execution exists, so its subject binding cannot be resolved; both editions **reject
  the combination at build time** with a pointed error. Re-open trigger: a
  child-binding channel (e.g. a pre-registered child binding derived from the parent's
  inheritance rule).
- **`DeleteWorkflowExecution` (complement, closed executions).** For **closed**
  executions the `TypefluxSubjectIds` index confirms are **subject-dedicated** (their
  subject set is exactly the target), `delete_executions_for_subject` /
  `deleteExecutionsForSubject` calls `DeleteWorkflowExecution` to remove history +
  visibility outright. It is BLUNT (deletes the whole execution, every subject on it),
  so exclusions are fail-safe and categorized: a **multi-subject** execution is excluded
  and reported by **count** (never the other subjects' ids); an execution whose subject
  set is **unreadable** is excluded as unanswerable; a readable set that **disowns the
  target** is excluded as `stale_index` (an index-integrity signal, never masked as a
  data-access problem); an execution whose **status** is absent/unrecognized is excluded
  too — closed-ness is decided against the SDK's status enum/name set, never a string
  default. **Running executions are reported, never touched** (terminate-then-delete is
  CLI territory, slice 5). Dry-run defaults ON.
- **Audit-honest, ids/counts only.** The report separates the **plan** (`deletable`)
  from the **authoritative outcome** (`deleted` / `deleted_count` + `failures`) — a
  consumer must never read the plan as a result — plus an **index-coverage caveat**:
  only executions stamped since slice 1 are enumerable, so the report is complete for
  post-slice-1 history only, never proof that no older executions exist. Serialized
  reports omit absent optional fields in both editions.
- **Rejected alternative (documented).** HKDF-**derived** keys
  (`HKDF(master, info=subject_id)`) were rejected: a derived key has no clean
  single-subject destroy — the master survives, so it is always re-derivable, and
  "erasing" it means rotating the master and re-encrypting every survivor. The keystore
  key-record scheme combines **real destroyable records**, not a master, so destroying a
  record permanently removes a `SHA-256` input and the sealing key is unreconstructable.

Both editions ship the same seams behaviorally (Python parity, not byte-identical), and
the binding conformance vectors pass in both. The `typeflux erase` operation that
orchestrates this alongside the Langfuse and cache surfaces is described below.

### Langfuse subject-trace deletion (#715 slice 2)

The Langfuse surface now has a **deletion primitive** — the first erasure lever
of #715 to ship. It removes every trace attributed to a subject id:

- **Dual-channel query.** Slice 1 stamps every trace with BOTH the portable
  `typeflux.subject:{id}` tag AND the native Langfuse `userId` (they always
  agree). Deletion queries by *both* carriers and takes the order-preserving,
  de-duplicated **union** of the ids — neither channel alone is authoritative, so
  a trace carrying only one is still caught. Each channel's result pages are
  walked **fully** (the trace-list API is page-numbered), so pagination never
  drops a match; if the page-cap safety ceiling truncates a scan, the report
  **warns explicitly** ("stopped after scanning N pages … more candidate pages
  remain") and covers only the traces found — never a silently partial set.
- **Fail-closed subject filter.** The listing NEVER runs unfiltered: if the
  trace-list API cannot accept a channel's subject-filter argument (`tags` /
  `user_id`), the operation raises a pointed error instead of listing — and
  deleting from — every trace in the window. Only genuinely-optional arguments
  (pagination, date bounds) tolerate API-surface differences.
- **Multi-subject traces are excluded, not collateral-deleted.** Slice 1 stamps
  a trace with ALL of a run's subjects; deleting such a trace while erasing ONE
  subject would silently destroy the other subjects' audit trails. Traces
  carrying other `typeflux.subject:` markers are excluded from deletion and
  reported in a `conflicted` list — each entry carries the trace id and a
  **count** of other subject markers, never the other subject ids (listing them
  would leak other subjects' presence into this subject's erasure report). A
  matched row whose tags are unreadable is excluded as conflicted-unknown —
  fail-safe, never deleted on a guess. The erase CLI (slice 5) surfaces
  conflicted traces for deliberate operator handling.
- **Dry-run by default.** The primitive defaults to `dry_run=True`: it returns the
  exact set of trace ids it *would* delete without mutating anything — the
  compliance plan an operator reviews before acting. A real run batches the bulk
  delete and reports what was deleted and, per batch, anything that failed
  (failures are recorded, never swallowed).
- **Report carries ids/counts only, never trace PII** — matched ids per channel,
  the deleted count, and any failures.
- **Index-coverage caveat (audit honesty).** The two query channels only see
  traces stamped with the subject index, which began with slice 1. **Traces
  written before subject tagging carry neither carrier and are invisible to this
  deletion** — the report always carries an explicit `index_coverage` note saying
  so. It is complete for post-slice-1 traces only, never proof that no older
  traces exist.
- **Not-supported is loud, never silent.** A backend with no deletable trace store
  (the no-op / in-memory readers, or an absent observability backend) raises a
  pointed not-supported error rather than falsely reporting a subject erased.

Both editions ship the same seam behaviorally (Python parity, not byte-identical):
Python `LangfuseTraceReader.delete_traces_for_subject(subject_id, *, dry_run, since, until)`
and TypeScript `deleteTracesForSubject(client, subjectId, { dryRun, since, until })`
both use their pinned langfuse SDK's native trace list + bulk-delete API. The
`typeflux erase` operation that orchestrates this and the other surfaces is described
below.

### Cache erasure (`SubjectErasableCacheStore`, #715 slice 3)

The cross-run cache memoizes an activity's **validated output** keyed by an opaque
`sha256` digest with no reverse index, so per-subject invalidation needs a write-time
index. A `CacheStore` opts in to the optional **`SubjectErasableCacheStore`** capability
(`erase_subject(subject_id, *, dry_run)` in Python / `eraseSubject(subjectId, { dryRun })`
in TypeScript) by maintaining a subject→key index at `set` time from each record's
`subjects` field (populated once subject plumbing, #715 slice 1, is in place). The
reference `InMemoryCacheStore` implements it in both editions; a store that does not is
capability-detected and yields a **not-supported** report that names the store class and
the blunt **full-flush fallback** (flush the whole store, or accept stale-but-inert
entries — a record holds validated output, not raw PII). The fallback is documented,
never automatic.

`dry_run` reports the affected keys (opaque digests — included in the report, not
sensitive) **without** deleting; executing deletes and reports the counts.

Because targeted-vs-flush is a wiring choice, it is **declarable and disclosed**
(#795): `runtime.cache_erasure: targeted` makes the capability a requirement —
wiring a store without it fails runtime assembly (the TS runtime's injected
`cacheStore`; the Python YAML run path wires no cross-run store today, so a
Python deployment satisfies the requirement vacuously — an empty cache has
nothing to erase), and the erase cache surface fails loudly both when the
wired store lacks the capability and when the surface is selected with no
store wired at all. The resolved bundle's `erasure` section states the
declared requirement and the resolution rule whenever a workflow declares
`subjects:` or `cache_erasure`; the receipt remains the record of which
behavior actually ran.

> **Coverage caveat (audit honesty).** Cache erasure only reaches records that carried a
> `subjects` field when they were written. Entries written **before** subject plumbing, or
> by a workflow that touched a subject's data through a path that declared no subject, carry
> no subject and are **invisible** to a per-subject erase — the report always states this,
> and a full store flush is the only way to guarantee their removal.

### The `typeflux erase` operation (#715 slice 5)

One operation fans subject-scoped deletion across every surface Typeflux controls and
emits the audit artifact. It is **library-first** (the #298 `admit_spec` precedent): the
seam is `erase_subject(subject_ids, *, actor, dry_run=True, surfaces=..., ...)` →
`ErasureReceipt` (top-level export of `typeflux`), and the CLI is a thin
wiring layer over it.

> **Erasure is a project-edition surface (recorded decision, #804).** There is
> deliberately no `yaml.erase` entry point: the receipt's value is its
> environment-resolved, attributed context — what the project layer provides —
> and a compliance-critical CLI should exist once, not per edition. A
> single-spec (yaml-edition) team adopts it by writing a one-manifest
> `typeflux.project.yaml` pointing at the existing YAML — cheaper than a second
> erase CLI would be to maintain. Re-open triggers: a production yaml-edition
> deployment with `subjects:` that cannot adopt a manifest, or a second
> edition-specific erasure surface appearing. The engine seams stay
> edition-independent, so nothing blocks a future wrapper.

```
typeflux-project erase typeflux.project.yaml \
    --workflow review --environment prod \
    --subject subject-0001 \
    [--surface temporal,langfuse,cache] [--since/--until ISO-8601] \
    [--dry-run | --execute --acknowledge-irreversible] \
    [--actor NAME] [--keystore-class module:Backend] \
    [--cache-store-class module:Store] [--json]
```

**The walkthrough — dry-run → review the receipt → execute:**

1. **Dry-run (the default).** `typeflux erase ... --subject subject-0001 --json`
   performs **zero mutation** and emits the full plan: the keystore is probed with a
   **non-minting introspection** (`subject_key_state` — a dry run never mints a record
   for an unseen subject and never leaves a tombstone), the execution driver
   enumerates without deleting, the trace driver lists without deleting, and the cache
   reads its index without deleting. The dry-run receipt IS the compliance plan an
   operator (or a DPO) reviews and signs.
2. **Review the receipt.** Same shape as an executed one, with the
   planned-vs-performed split inside each surface: `shreddable_key_records` vs
   `shredded_key_records`, `deletable` vs `deleted`, `trace_ids` vs `deleted_count`,
   `keys_found` vs `keys_deleted`. Check the **warnings**: conflicted (multi-subject)
   executions/traces excluded fail-safe, still-running executions (reported, **never**
   touched — erase does not terminate; re-run after they close or cancel/migrate them
   first), enumeration truncation, and window notes.
3. **Execute.** `--execute` mutates; because destroyed key records and deleted
   histories are unrecoverable, executing the **temporal** surface additionally
   requires **`--acknowledge-irreversible`** (the `--abandon-gates` acknowledgment
   pattern). A failed surface is unmistakable: it is marked `failed` in the receipt
   and the CLI exits non-zero. One bad surface never aborts the others — every
   surface's outcome (or failure) is recorded.

**Surface wiring.** The workflow/environment selection supplies the Temporal profile
and observability backend exactly like `migrate`/`drain-status`. The keystore and
cache store are **code-injected**: `--keystore-class` / `--cache-store-class` name
zero-arg-constructible backends (Python) that read their own configuration from the
environment — unlike spec-declared `type: custom` extensions they have no `config:`
block, because they are CLI arguments rather than spec surfaces (a spec-side keystore
config belongs to the `payload_codec.subject_scope` design if a backend ever needs
one). The TS CLI's `--bindings` names the same
module a deployed worker uses (`TYPEFLUX_WORKER_BINDINGS`, exporting
`{ subjectKeystore?, cacheStore? }`). A selected surface whose dependency is not
configured (no keystore backend, no Langfuse credentials, no cache store) is reported
**skipped with an explicit reason** — never silently omitted, never a crash. The
process-local in-memory reference backends hold no records minted elsewhere, so the
CLI never fabricates one.

**Windowing.** `--since`/`--until` bound the **Langfuse trace scan** only. The
temporal and cache surfaces are window-less (the subject index and key records carry
no time dimension); selecting them alongside a window adds an explicit note to the
receipt, and a window with no windowable surface selected is a loud error.

**The receipt is the proof — store it OUTSIDE the erased surfaces.** `--json` emits
the `ErasureReceipt` to stdout for the caller to persist (a compliance ledger, an
audit bundle store — anywhere that is *not* one of the surfaces just erased). It
carries **ids and counts only**: never erased content, and never other subjects' ids
(multi-subject conflicts are counts). Every receipt names the subject ids, ISO-8601
timestamp, the **actor** (defaults to the local OS user; pass `--actor` for a real
principal), the dry-run flag, all three surface reports, aggregated warnings, and the
always-present **`unreachable`** block:

- **`provider_logs`** — provider-owned; erase via your model provider's
  retention/deletion controls.
- **`exported_artifacts`** — caller-owned; exported manifests/bundles are the
  exporter's responsibility.
- **`mixed_workflow_payloads`** — the documented granularity limit: a mixed-subject
  execution's payloads are sealed under a key combined from ALL its subjects'
  records, so erasing any member shreds the shared history wholesale — never one
  subject's slice of it.

One remaining backup caveat: ciphertext of **no-subject** executions (and any history
sealed before subject scoping was enabled) lives under the shared codec key, so
Temporal backups retain it recoverable until that key is rotated/destroyed — the
keystore shred closes this only for subject-scoped payloads.

**Control-plane intake is deferred** (the #298 Phase-C precedent): erasure is a
stateful multi-surface fan-out producing an audit artifact, which fits a CLI/library
operation, not the stateless re-read-per-request control plane. Re-open trigger: a
hosted-console "erase subject" button — it then follows the #616 contract-first flow
(contract edit + conforming Python + `schema.ts` regen + client version bump in one
PR).

### `artifacts.max_bytes` is not retention

`artifacts.max_bytes` is a **size ceiling** on inlined artifact bytes (an ingestion guard),
**not** a retention or deletion control. It bounds how much rides a payload; it says nothing
about how long data is kept. Do not treat it as an erasure or TTL mechanism.

---

See also: [Observability](observability.md) (redaction & trace shape),
[Compliance Readiness](compliance-readiness.md) (the feature → certification map),
[Control Plane](control-plane.md) (admission & review signals).
