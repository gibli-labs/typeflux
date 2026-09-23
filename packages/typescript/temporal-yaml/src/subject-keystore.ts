/**
 * Per-subject crypto-shred keystore + subject-scoped codec seam (#715 slice 4) —
 * behavioral parity with the Python `yaml/subject_keystore.py`.
 *
 * The shared-key payload codec (`payload-codec.ts`) is content-blind: it seals every
 * payload under one `current` key, so crypto-shred there is all-or-nothing per key id,
 * never per subject. This module makes per-subject shred real, per the ratified §4.1
 * decision (option **ii**: per-subject KEYSTORE KEY RECORDS):
 *
 * - a `SubjectKeystore` maps `subjectId -> 32-byte record`, minted on first use, with
 *   `destroySubjectKey` as the erasure primitive — destroying a record renders that
 *   subject's ciphertext permanently unreadable (survives Temporal backups);
 * - a `SubjectScopedPayloadCodec` wraps the shared-key codec and resolves the OWNING
 *   EXECUTION's subject ids per encode, sealing subject-scoped payloads under a key
 *   DERIVED FROM the execution's per-subject records so destroying ANY member subject's
 *   record shreds the payload (AND-semantics for mixed executions).
 *
 * **How the codec learns the execution (the wired seam).** The Temporal TS SDK passes a
 * `SerializationContext` (workflow / activity, carrying `workflowId`) as the second
 * argument of `PayloadCodec.encode`/`decode` on every standard client and worker path.
 * The codec resolves `workflowId -> subject ids` through `SubjectKeyBindings`:
 *
 * 1. the **registry** — the runtime start path registers the ids it stamps into
 *    `TypefluxSubjectIds` (including an explicit "no subjects" entry) before starting;
 * 2. the **visibility fallback** — a registry miss (another process started the
 *    execution) describes the execution through the late-bound client and reads its
 *    `TypefluxSubjectIds` search attribute (the erasure index itself); cached;
 * 3. anything else **fails closed** — no context, no binding, no client means subject
 *    scoping cannot be honored, and the codec throws rather than silently sealing a
 *    subject's payload under the shared key.
 *
 * Why keystore records, not HKDF-derived keys (option **i**, REJECTED): a derived key
 * `HKDF(master, info=subjectId)` cannot be single-subject-destroyed — the master
 * survives, so it is always re-derivable. `combineSubjectKey` takes REAL keystore
 * records as inputs (not a master), so destroying a record permanently removes a
 * SHA-256 input and the sealing key is unreconstructable.
 *
 * Fail-closed: a DESTROYED subject fails closed on encode (never re-minted); decode of
 * a subject-scoped payload whose record is destroyed throws the DISTINCT
 * `SubjectKeyShreddedError` (never plaintext, never a silent empty); subject ids MUST
 * NOT be recycled after erasure (the tombstone is permanent).
 *
 * **Deployment boundary (documented, not silent):** `InMemorySubjectKeystore` and the
 * binding registry are PROCESS-LOCAL. A starter and a worker in different processes
 * with independent in-memory keystores mint DIFFERENT keys for the same subject —
 * cross-process deployments MUST inject a shared `SubjectKeystore` backend
 * (`BuildRuntimeOptions.subjectKeystore`). Bindings resolve cross-process out of the
 * box via the visibility fallback. Sub-workflow composition is NOT yet supported under
 * `subject_scope` (a child's input is encoded before the child exists); the runtime
 * rejects the combination at build time.
 *
 * **Atomicity note (parity with Python's threading.Lock)**: JS is single-threaded and
 * the keystore methods contain NO `await`, so each mint/lookup/destroy runs to
 * completion without interleaving — the compound record+tombstone updates are atomic by
 * the event loop. A future async-backed keystore MUST re-establish this (serialize
 * destroy against a concurrent first-use mint) or it can leave a live key for an
 * erased subject.
 */

import { createHash } from "node:crypto";
import { randomBytes } from "node:crypto";

import type { Payload, PayloadCodec, SerializationContext } from "@temporalio/common";
import { defineSearchAttributeKey, SearchAttributeType } from "@temporalio/common";
import proto from "@temporalio/proto";

import {
  buildPayloadCodec,
  ENCRYPTED_ENCODING,
  KEY_ID_METADATA_KEY,
  KEY_LEN,
  NONCE_LEN,
  openPayloadBytes,
  PayloadCodecError,
  RESERVED_KID_PREFIX,
  sealPayloadBytes,
  type TypefluxAesGcmPayloadCodec,
} from "./payload-codec.js";
import type { PayloadCodecSpec } from "./spec.js";
import { normalizeSubjectIds, SUBJECT_IDS_SEARCH_ATTRIBUTE } from "./subjects.js";

const PayloadProto = proto.temporal.api.common.v1.Payload;

// --- Errors ---------------------------------------------------------------------------

/**
 * Decode/encode hit a subject whose key record has been SHREDDED (#715 slice 4). A
 * DISTINCT, catchable error (extends `PayloadCodecError`, so broad fail-closed codec
 * handlers still catch it — Python parity) that NAMES the shred rather than surfacing
 * as a generic auth failure. Its presence means the subject was erased: the ciphertext
 * is intact but permanently unreadable — never a plaintext passthrough, never a silent
 * empty.
 */
export class SubjectKeyShreddedError extends PayloadCodecError {
  readonly subjectId: string;
  constructor(subjectId: string) {
    super(
      `subject '${subjectId}' key record has been shredded (erased, #715): its payloads ` +
        "are permanently unreadable — this is the crypto-shred, not a transient crypto failure",
    );
    this.name = "SubjectKeyShreddedError";
    this.subjectId = subjectId;
  }
}

/**
 * A subject-scoped decode named a subject with NO key record (never created). Distinct
 * from `SubjectKeyShreddedError` (erased): the keystore has no memory of the subject —
 * a wrong/foreign keystore (e.g. a process-local in-memory keystore that did not mint
 * the key), or a payload from another deployment. Still fail-closed.
 */
export class SubjectKeyUnknownError extends PayloadCodecError {
  readonly subjectId: string;
  constructor(subjectId: string) {
    super(
      `no key record for subject '${subjectId}' (never created and not a shred); cannot ` +
        "decode a payload sealed for it — if this process is not the one that minted the " +
        "key, configure a SHARED SubjectKeystore backend (the in-memory reference " +
        "keystore is process-local)",
    );
    this.name = "SubjectKeyUnknownError";
    this.subjectId = subjectId;
  }
}

// --- The keystore seam ----------------------------------------------------------------

/** Outcome of a `destroySubjectKey` call (#715 slice 4). `keyExisted` is true when a
 * LIVE key was actually shredded; `alreadyDestroyed` marks an idempotent repeat. */
export interface SubjectKeyDestructionResult {
  readonly subjectId: string;
  readonly keyExisted: boolean;
  readonly alreadyDestroyed: boolean;
}

/** The three states a subject's key record can be in — the return of the NON-MINTING
 * probe the erasure dry-run reads (#715 slice 5): `live` = a real key exists,
 * `destroyed` = a permanent shred tombstone, `absent` = never keyed. */
export type SubjectKeyState = "live" | "destroyed" | "absent";

/**
 * Maps `subjectId -> 32-byte data-key record` with a real single-subject destroy.
 *
 * - `dataKey(subjectId, { create: true })` (encode) mints on first use, returns the
 *   existing key thereafter, but throws `SubjectKeyShreddedError` for a destroyed
 *   subject (never re-mints).
 * - `dataKey(subjectId, { create: false })` (decode) returns the live key, throws
 *   `SubjectKeyShreddedError` for a destroyed subject, `SubjectKeyUnknownError` for one
 *   never created.
 * - `destroySubjectKey(subjectId)` drops the key bytes and leaves a permanent tombstone
 *   (idempotent). Destroyed keys are UNRECOVERABLE through this API.
 */
export interface SubjectKeystore {
  dataKey(subjectId: string, options: { create: boolean }): Buffer;
  destroySubjectKey(subjectId: string): SubjectKeyDestructionResult;
  /**
   * OPTIONAL non-minting, non-destroying existence probe (#715 slice 5): the erasure
   * dry-run must report whether a record WOULD be destroyed without minting one
   * (mint-on-first-use is an encode-path concern) and without leaving a tombstone.
   * A backend without it is still driven correctly — the module-level
   * {@link subjectKeyState} helper falls back to a read-only `dataKey({create:false})`
   * probe.
   */
  subjectKeyState?(subjectId: string): SubjectKeyState;
}

/** Structural type-guard checking EVERY method of `SubjectKeystore` (#715 review bar). */
export function isSubjectKeystore(value: unknown): value is SubjectKeystore {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.dataKey === "function" && typeof candidate.destroySubjectKey === "function";
}

/**
 * Process-local reference `SubjectKeystore`. For tests and single-process use ONLY:
 * two processes each holding an in-memory keystore mint DIFFERENT keys for the same
 * subject — cross-process deployments must back the same interface with a shared store
 * (KMS/Vault/Postgres).
 *
 * ONE map holds both live records and tombstones — a `Buffer` is a live key, `null` is
 * a destroyed subject's PERMANENT tombstone — so the two states can never desync under
 * future mutation paths. Keys are stored AND returned as defensive copies: a caller
 * mutating a returned buffer can never corrupt the stored record (which would make
 * history unreadable with no tombstone/audit signal).
 */
export class InMemorySubjectKeystore implements SubjectKeystore {
  // subjectId -> live 32-byte key, or null = destroyed (permanent tombstone).
  private readonly records = new Map<string, Buffer | null>();

  dataKey(subjectId: string, options: { create: boolean }): Buffer {
    if (subjectId.length === 0) {
      // Fail closed: an empty id would collide the whole "no subject" namespace.
      throw new PayloadCodecError("subject id for a data key must be a non-empty string");
    }
    if (this.records.has(subjectId)) {
      const existing = this.records.get(subjectId)!;
      if (existing === null) throw new SubjectKeyShreddedError(subjectId);
      // Defensive copy — caller mutation cannot corrupt the stored record.
      return Buffer.from(existing);
    }
    if (!options.create) throw new SubjectKeyUnknownError(subjectId);
    const key = randomBytes(KEY_LEN);
    this.records.set(subjectId, Buffer.from(key));
    return key;
  }

  destroySubjectKey(subjectId: string): SubjectKeyDestructionResult {
    if (subjectId.length === 0) {
      throw new PayloadCodecError("subject id to destroy must be a non-empty string");
    }
    const existing = this.records.get(subjectId);
    const alreadyDestroyed = this.records.has(subjectId) && existing === null;
    const keyExisted = existing !== null && existing !== undefined;
    // Drop the key BYTES (unrecoverable) and leave a permanent tombstone so a future
    // encode fails closed rather than re-minting the erased subject.
    this.records.set(subjectId, null);
    return { subjectId, keyExisted, alreadyDestroyed };
  }

  /**
   * NON-MINTING, NON-DESTROYING existence probe for the erasure dry-run (#715
   * slice 5): reads the record map without ever creating a record or a tombstone.
   * `destroyed` and `absent` differ so the plan distinguishes an already-shredded
   * subject from one that was never keyed.
   */
  subjectKeyState(subjectId: string): SubjectKeyState {
    if (subjectId.length === 0) {
      throw new PayloadCodecError("subject id to probe must be a non-empty string");
    }
    if (!this.records.has(subjectId)) return "absent";
    return this.records.get(subjectId) === null ? "destroyed" : "live";
  }

  /** Test-only: seed a record so a codec seals bytes matching a pinned conformance
   * vector. NOT part of the `SubjectKeystore` interface. */
  seedRecordForTest(subjectId: string, key: Buffer): void {
    this.records.set(subjectId, Buffer.from(key));
  }
}

/**
 * Probe a keystore for a subject's key state WITHOUT minting or destroying (#715
 * slice 5). Prefers the backend's own optional `subjectKeyState`; a backend that
 * predates it falls back to a read-only `dataKey(subjectId, { create: false })` probe
 * — which never mints (`create: false`) and never destroys — mapping its fail-closed
 * typed errors to the three states. Either way a dry run leaves the keystore
 * byte-for-byte unchanged.
 */
export function subjectKeyState(keystore: SubjectKeystore, subjectId: string): SubjectKeyState {
  if (typeof keystore.subjectKeyState === "function") {
    return keystore.subjectKeyState(subjectId);
  }
  try {
    keystore.dataKey(subjectId, { create: false });
  } catch (error) {
    if (error instanceof SubjectKeyShreddedError) return "destroyed";
    if (error instanceof SubjectKeyUnknownError) return "absent";
    throw error;
  }
  return "live";
}

// --- The subject-key wire scheme (byte-pinned in binding.v1.json) ---------------------

/** Build the subject-scoped `typeflux-key-id` for an execution's subject set:
 * `tfsubj1:<b64url(s1)>,<b64url(s2)>,...` — the scheme marker, then each subject id
 * base64url-encoded (no padding, delimiter-safe), comma-joined in resolved order. */
export function subjectKid(subjectIds: readonly string[]): string {
  if (subjectIds.length === 0) {
    throw new PayloadCodecError("cannot build a subject-scoped key id for an empty subject set");
  }
  const parts = subjectIds.map((id) => Buffer.from(id, "utf8").toString("base64url"));
  return RESERVED_KID_PREFIX + parts.join(",");
}

/** Parse a subject-scoped `typeflux-key-id` back to its ordered subject set. Throws
 * (fail-closed) for a kid that lacks the scheme marker or is malformed. CANONICAL
 * segments only (re-encode must reproduce the segment) — kid metadata is
 * unauthenticated, so both editions accept exactly one encoding per subject set. */
export function parseSubjectKid(kid: string): string[] {
  if (!kid.startsWith(RESERVED_KID_PREFIX)) {
    throw new PayloadCodecError(`key id '${kid}' is not a subject-scoped key id`);
  }
  const body = kid.slice(RESERVED_KID_PREFIX.length);
  if (body.length === 0) {
    throw new PayloadCodecError(`subject-scoped key id '${kid}' names no subjects`);
  }
  return body.split(",").map((part) => {
    if (part.length === 0) {
      throw new PayloadCodecError(`subject-scoped key id '${kid}' has an empty subject segment`);
    }
    // base64url round-trip check: re-encoding must reproduce the segment, else it is not
    // a canonical encoding (Node's decoder is lenient, so verify explicitly — fail-closed).
    const decoded = Buffer.from(part, "base64url");
    if (decoded.toString("base64url") !== part) {
      throw new PayloadCodecError(`subject-scoped key id '${kid}' has an undecodable subject segment '${part}'`);
    }
    return decoded.toString("utf8");
  });
}

/** Combine an execution's per-subject record keys into ONE 32-byte AES key:
 * `SHA-256(key(s1) || key(s2) || ...)` in kid order. The inputs are real keystore
 * records (not a re-derivable master), so destroying ANY member permanently removes a
 * SHA-256 input and the combined key is unreconstructable. */
export function combineSubjectKey(recordKeys: readonly Buffer[]): Buffer {
  if (recordKeys.length === 0) {
    throw new PayloadCodecError("cannot combine an empty set of subject key records");
  }
  const hasher = createHash("sha256");
  for (const recordKey of recordKeys) {
    if (recordKey.length !== KEY_LEN) {
      throw new PayloadCodecError(`a subject key record must be exactly ${KEY_LEN} bytes (got ${recordKey.length})`);
    }
    hasher.update(recordKey);
  }
  return hasher.digest();
}

// --- Subject bindings: workflowId -> subject ids --------------------------------------

/** The client slice the visibility fallback needs (`@temporalio/client` shape). */
export interface SubjectBindingClient {
  workflow: {
    getHandle(workflowId: string): {
      describe(): Promise<{
        typedSearchAttributes?: {
          get(key: unknown): unknown;
        };
      }>;
    };
  };
}

/**
 * Resolves `workflowId -> subject ids` for the subject-scoped codec. Two channels, in
 * order: the process-local REGISTRY (fed by the runtime start path with the same ids it
 * stamps into `TypefluxSubjectIds`; an empty array pins "no subjects"), then the
 * VISIBILITY fallback (describe the execution through the late-bound client and read
 * the search attribute — the erasure index itself; cached). A miss with no bound
 * client, or a describe failure, FAILS CLOSED.
 */
export class SubjectKeyBindings {
  private readonly known = new Map<string, readonly string[]>();
  private client: SubjectBindingClient | undefined;

  /** Pin an execution's subject set (called by the start path, pre-start). */
  register(workflowId: string, subjectIds: readonly string[]): void {
    if (workflowId.length === 0) {
      throw new PayloadCodecError("cannot register a subject binding for an empty workflow id");
    }
    this.known.set(workflowId, normalizeSubjectIds(subjectIds));
  }

  /** Late-bind the connected Temporal client for the visibility fallback. */
  bindClient(client: SubjectBindingClient): void {
    this.client = client;
  }

  /** Resolve an execution's subject ids: registry, then visibility, else throw. */
  async resolve(workflowId: string): Promise<readonly string[]> {
    const known = this.known.get(workflowId);
    if (known !== undefined) return known;
    const client = this.client;
    if (client === undefined) {
      throw new PayloadCodecError(
        `cannot resolve subject ids for workflow '${workflowId}': it was not registered by ` +
          "this process's start path and no Temporal client is bound for the visibility " +
          "fallback — subject scoping fails closed rather than sealing under the shared " +
          "key (#715 slice 4)",
      );
    }
    let description: { typedSearchAttributes?: { get(key: unknown): unknown } };
    try {
      description = await client.workflow.getHandle(workflowId).describe();
    } catch (error) {
      const name = error instanceof Error ? error.name : "Error";
      throw new PayloadCodecError(
        `cannot resolve subject ids for workflow '${workflowId}': the visibility describe ` +
          `failed (${name}). If this is a child workflow being scheduled, note that ` +
          "sub-workflow composition is not yet supported under " +
          "payload_codec.subject_scope (#715 slice 4)",
      );
    }
    const subjects = describedSubjectIds(description);
    this.known.set(workflowId, subjects);
    return subjects;
  }
}

/** Read `TypefluxSubjectIds` off a workflow describe result; absent ⇒ no subjects. */
function describedSubjectIds(description: {
  typedSearchAttributes?: { get(key: unknown): unknown };
}): readonly string[] {
  try {
    const typed = description.typedSearchAttributes;
    if (typed === undefined) return [];
    const key = defineSearchAttributeKey(SUBJECT_IDS_SEARCH_ATTRIBUTE, SearchAttributeType.KEYWORD_LIST);
    const value = typed.get(key);
    if (!Array.isArray(value)) return [];
    return value.map((item) => String(item));
  } catch {
    return [];
  }
}

// --- The subject-scoped codec ---------------------------------------------------------

/**
 * A payload codec that seals subject-scoped executions under per-subject keys. Wraps
 * the shared-key `TypefluxAesGcmPayloadCodec`, a `SubjectKeystore`, and a
 * `SubjectKeyBindings`. The SDK passes the owning execution's `SerializationContext`
 * per operation; per encode the codec resolves the execution's subject ids:
 * no subjects ⇒ delegates to the shared codec (byte-identical to #188); one or more ⇒
 * mints/fetches each record, combines them, and seals every payload of the call under
 * that combined key with a `tfsubj1:...` kid; NO context (or one without a workflowId)
 * ⇒ FAILS CLOSED. Decode routes on the kid prefix and needs no binding resolution.
 */
export class SubjectScopedPayloadCodec implements PayloadCodec {
  readonly bindings: SubjectKeyBindings;

  constructor(
    private readonly base: TypefluxAesGcmPayloadCodec,
    readonly keystore: SubjectKeystore,
    bindings?: SubjectKeyBindings,
  ) {
    this.bindings = bindings ?? new SubjectKeyBindings();
  }

  async encode(payloads: Payload[], context?: SerializationContext): Promise<Payload[]> {
    const workflowId = contextWorkflowId(context);
    if (workflowId === undefined) {
      // Fail closed: no serialization context ⇒ the owning execution (and so the
      // subject set) is unknowable; guessing shared-key could leak a subject's payload
      // outside its shred domain.
      throw new PayloadCodecError(
        "subject-scoped payload codec cannot encode without a serialization context " +
          "naming the owning execution (#715 slice 4); this encode path did not supply one",
      );
    }
    const subjectIds = await this.bindings.resolve(workflowId);
    if (subjectIds.length === 0) {
      // No subjects: shared-key path, byte-identical to #188.
      return this.base.encode(payloads);
    }
    const kid = subjectKid(subjectIds);
    const combined = combineSubjectKey(subjectIds.map((id) => this.keystore.dataKey(id, { create: true })));
    return payloads.map((payload) =>
      sealPayloadBytes({
        key: combined,
        kid,
        nonce: randomBytes(NONCE_LEN),
        plaintext: Buffer.from(PayloadProto.encode(payload).finish()),
      }),
    );
  }

  async decode(payloads: Payload[], _context?: SerializationContext): Promise<Payload[]> {
    const out: Payload[] = [];
    // Per-CALL combined-key cache: a batch of N payloads under one kid costs one
    // keystore fetch + combine, not N (a real KMS-backed keystore pays a network round
    // trip per fetch). The cache does NOT outlive this call, so a destroy between
    // calls is always observed (destroy-safety preserved).
    const combinedByKid = new Map<string, Buffer>();
    for (const payload of payloads) {
      const encoding = payload.metadata?.encoding;
      const kidBytes = payload.metadata?.[KEY_ID_METADATA_KEY];
      const isSubjectScoped =
        encoding !== undefined &&
        encoding !== null &&
        Buffer.from(encoding).toString("utf8") === ENCRYPTED_ENCODING &&
        kidBytes !== undefined &&
        kidBytes !== null &&
        Buffer.from(kidBytes).toString("utf8").startsWith(RESERVED_KID_PREFIX);
      if (!isSubjectScoped) {
        // Shared-key encrypted, or a pre-codec passthrough: the wrapped codec applies
        // its own fail-closed / passthrough discipline.
        out.push((await this.base.decode([payload]))[0]!);
        continue;
      }
      const kid = Buffer.from(kidBytes!).toString("utf8");
      let combined = combinedByKid.get(kid);
      if (combined === undefined) {
        const subjectIds = parseSubjectKid(kid);
        // dataKey(create:false) throws SubjectKeyShreddedError for an erased subject
        // (naming the shred) or SubjectKeyUnknownError for one never created — either
        // way fail-closed, never a plaintext passthrough, never a silent empty.
        combined = combineSubjectKey(subjectIds.map((id) => this.keystore.dataKey(id, { create: false })));
        combinedByKid.set(kid, combined);
      }
      const innerBytes = openPayloadBytes({ key: combined, data: Buffer.from(payload.data ?? new Uint8Array()) });
      out.push(PayloadProto.decode(innerBytes));
    }
    return out;
  }
}

/** Extract the owning workflow id from an SDK serialization context, if any. */
function contextWorkflowId(context: SerializationContext | undefined): string | undefined {
  if (context === undefined) return undefined;
  if (context.type === "workflow") return context.workflowId;
  if (context.type === "activity") return context.workflowId;
  return undefined;
}

/**
 * The ONE codec-assembly helper the runtime build sites use (#715 fix round, item 1):
 * builds the shared AES codec from the spec block and — when `subject_scope` is
 * declared — wraps it in the `SubjectScopedPayloadCodec` with the injected keystore.
 * `undefined` spec ⇒ no codec, exactly as before.
 *
 * FAIL-CLOSED (#715 Bugbot): `subject_scope` with NO keystore throws — this helper
 * never silently mints a process-local keystore, because most callers are NOT the
 * sole owner of subject key records (a CP/reader process sealing under freshly-minted
 * keys produces payloads no worker can decode). The ONLY site allowed to supply the
 * in-memory reference default is `buildRuntime`, which owns both the worker and the
 * start-path converter in one process (see its sole-owner comment).
 */
export function buildSubjectAwarePayloadCodec(
  spec: PayloadCodecSpec | undefined,
  keystore?: SubjectKeystore,
): TypefluxAesGcmPayloadCodec | SubjectScopedPayloadCodec | undefined {
  const base = buildPayloadCodec(spec);
  if (base === undefined) return undefined;
  if (spec?.subject_scope === undefined) return base;
  if (keystore === undefined) {
    throw new PayloadCodecError(
      "runtime.temporal.payload_codec.subject_scope requires an injected SubjectKeystore " +
        "backend on this path (#715 slice 4): this process is not the sole owner of subject " +
        "key records, and a silently-minted process-local keystore would seal subject " +
        "payloads under keys no worker holds. Inject a SHARED keystore backend " +
        "(BuildRuntimeOptions.subjectKeystore for the runtime-owned worker path; see " +
        "docs/typescript/privacy.md).",
    );
  }
  return new SubjectScopedPayloadCodec(base, keystore);
}
