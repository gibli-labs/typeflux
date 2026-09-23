/**
 * AES-256-GCM Temporal payload codec (#188 slice 1) — the TS twin of the Python
 * `yaml/payload_codec.py`. The codec encrypts the WHOLE serialized `Payload` proto
 * (Temporal's reference-codec pattern) so every workflow/activity IO value and
 * review-signal payload rides Temporal history as ciphertext.
 *
 * Wire layout is PINNED in `contracts/temporal-binding/binding.v1.json` and byte-identical
 * to the Python edition, so a payload encrypted by one edition decrypts in the other under
 * the same key:
 *
 * - the encrypted `Payload` carries `metadata["encoding"] = "binary/encrypted"` and
 *   `metadata["typeflux-key-id"] = <kid utf-8 bytes>`;
 * - `data = nonce(12 random bytes) || ciphertext || tag(16 bytes)`, AES-256-GCM over the
 *   serialized inner `Payload` proto with NO associated data.
 *
 * Fail-closed (D188-3): an UNKNOWN key id or an auth-tag failure throws — never a plaintext
 * passthrough. A payload with no `binary/encrypted` marker passes `decode` through untouched
 * (pre-codec / mixed history). Key VALUES are never logged; the key slots join
 * `SECRET_SLOT_PATHS` so bundles/plans mask them to source_kind/source_name.
 */

import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import type { Payload, PayloadCodec } from "@temporalio/common";
// `@temporalio/proto` is a CommonJS package (protobufjs-generated, no static named exports),
// so a default import is the interop-safe form under `verbatimModuleSyntax` ESM output.
import proto from "@temporalio/proto";

import type { PayloadCodecKeySpec, PayloadCodecSpec } from "./spec.js";

const PayloadProto = proto.temporal.api.common.v1.Payload;

/**
 * A codec load/encrypt/decrypt failure — always fail-closed, never a passthrough
 * (Python `PayloadCodecError` parity, #715 fix round). Every throw in this module and
 * the subject-keystore errors share this base, so a broad catch-and-fail-closed caller
 * has ONE marker type in both editions.
 */
export class PayloadCodecError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PayloadCodecError";
  }
}

// --- Pinned wire constants (identical in the Python edition) --------------------------

/** `metadata["encoding"]` marker on an encrypted payload. */
export const ENCRYPTED_ENCODING = "binary/encrypted";
/** `metadata` key carrying the encrypting key's id (utf-8 bytes). */
export const KEY_ID_METADATA_KEY = "typeflux-key-id";
/** AES-GCM nonce length, bytes. A FRESH CSPRNG value per encrypt — never reused. */
export const NONCE_LEN = 12;
/** AES-GCM authentication tag length, bytes. */
export const TAG_LEN = 16;
/** AES-256 key length, bytes. */
export const KEY_LEN = 32;

/** Wire scheme marker for a SUBJECT-scoped `typeflux-key-id` (#715 slice 4). A
 * subject-scoped payload's key id is `tfsubj1:<b64url(s1)>,<b64url(s2)>,...`; a
 * shared-key (no-subject) payload keeps its plain configured kid, byte-for-byte as
 * before slice 4. The two kid namespaces are kept DISJOINT: a configured codec key id
 * may not start with `RESERVED_KID_PREFIX`, so decode routes unambiguously on the
 * prefix. Byte-pinned in `binding.v1.json` (`payload_codec.subject_key_scheme`). */
export const SUBJECT_KID_SCHEME = "tfsubj1";
/** The reserved prefix (`"tfsubj1:"`) a configured shared key id must NOT use. */
export const RESERVED_KID_PREFIX = `${SUBJECT_KID_SCHEME}:`;

// --- AES-256-GCM primitives (byte-pinned, shared by codec + conformance vectors) ------

/** Seal a serialized inner `Payload` (`plaintext`) into an encrypted `Payload`. The
 * caller supplies `nonce` so this is deterministic (conformance vectors pin a fixed
 * nonce); the production codec always passes a fresh CSPRNG nonce. */
export function sealPayloadBytes(args: {
  key: Buffer;
  kid: string;
  nonce: Buffer;
  plaintext: Buffer;
}): Payload {
  if (args.key.length !== KEY_LEN) throw new PayloadCodecError("AES-256 key must be exactly 32 bytes");
  if (args.nonce.length !== NONCE_LEN) throw new PayloadCodecError("AES-GCM nonce must be exactly 12 bytes");
  const cipher = createCipheriv("aes-256-gcm", args.key, args.nonce);
  const ciphertext = Buffer.concat([cipher.update(args.plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    metadata: {
      encoding: Buffer.from(ENCRYPTED_ENCODING, "utf8"),
      [KEY_ID_METADATA_KEY]: Buffer.from(args.kid, "utf8"),
    },
    data: Buffer.concat([args.nonce, ciphertext, tag]),
  };
}

/** Open the `data` (`nonce || ciphertext || tag`) of an encrypted payload, returning the
 * serialized inner `Payload` bytes. Throws on an auth-tag failure — never returns garbage. */
export function openPayloadBytes(args: { key: Buffer; data: Buffer }): Buffer {
  if (args.data.length < NONCE_LEN + TAG_LEN) {
    throw new PayloadCodecError("encrypted payload data is too short to hold a nonce and tag");
  }
  const nonce = args.data.subarray(0, NONCE_LEN);
  const tag = args.data.subarray(args.data.length - TAG_LEN);
  const ciphertext = args.data.subarray(NONCE_LEN, args.data.length - TAG_LEN);
  const decipher = createDecipheriv("aes-256-gcm", args.key, nonce);
  decipher.setAuthTag(tag);
  try {
    return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  } catch {
    throw new PayloadCodecError("encrypted payload failed authentication (wrong key or tampered)");
  }
}

// --- The composite codec --------------------------------------------------------------

/** The ONE composite AES-256-GCM codec wrapped as the sole entry of the worker data
 * converter's `payloadCodecs` array — cross-edition interop rests only on the pinned wire
 * format, not on chain semantics. */
export class TypefluxAesGcmPayloadCodec implements PayloadCodec {
  private readonly currentKid: string;
  private readonly keys: Map<string, Buffer>;

  constructor(args: { currentKid: string; keys: Map<string, Buffer> }) {
    if (!args.keys.has(args.currentKid)) {
      throw new PayloadCodecError("payload codec current key id is not among the resolved keys");
    }
    for (const [kid, value] of args.keys) {
      if (kid.startsWith(RESERVED_KID_PREFIX)) {
        throw new PayloadCodecError(
          `payload codec key id '${kid}' may not use the reserved subject-key prefix '${RESERVED_KID_PREFIX}' (#715 slice 4)`,
        );
      }
      if (value.length !== KEY_LEN) {
        // Never log the value; name + length only.
        throw new PayloadCodecError(`payload codec key '${kid}' must be exactly ${KEY_LEN} bytes (got ${value.length})`);
      }
    }
    this.currentKid = args.currentKid;
    this.keys = new Map(args.keys);
  }

  async encode(payloads: Payload[]): Promise<Payload[]> {
    const key = this.keys.get(this.currentKid);
    if (key === undefined) throw new PayloadCodecError("payload codec current key missing");
    return payloads.map((payload) =>
      sealPayloadBytes({
        key,
        kid: this.currentKid,
        nonce: randomBytes(NONCE_LEN),
        plaintext: Buffer.from(PayloadProto.encode(payload).finish()),
      }),
    );
  }

  async decode(payloads: Payload[]): Promise<Payload[]> {
    return payloads.map((payload) => {
      const encoding = payload.metadata?.encoding;
      if (encoding === undefined || encoding === null || Buffer.from(encoding).toString("utf8") !== ENCRYPTED_ENCODING) {
        // Pre-codec / non-encrypted payload: pass through untouched.
        return payload;
      }
      const kidBytes = payload.metadata?.[KEY_ID_METADATA_KEY];
      if (kidBytes === undefined || kidBytes === null) {
        throw new PayloadCodecError("encrypted payload carries no typeflux-key-id");
      }
      const kid = Buffer.from(kidBytes).toString("utf8");
      const key = this.keys.get(kid);
      if (key === undefined) {
        // Unknown key id: fail closed, never passthrough.
        throw new PayloadCodecError(`no key registered for encrypted payload key id '${kid}'`);
      }
      const innerBytes = openPayloadBytes({ key, data: Buffer.from(payload.data ?? new Uint8Array()) });
      return PayloadProto.decode(innerBytes);
    });
  }
}

// --- Key resolution + builder ---------------------------------------------------------

/** `~`-expansion for a configured secret-file path (the ts driver's rule). */
const expandHome = (path: string): string =>
  path.startsWith("~") ? join(homedir(), path.slice(1).replace(/^\//, "")) : path;

/** Resolve a codec key's value_from to bytes: env text (trimmed, utf-8) or file bytes.
 * Fail-closed — a missing/empty source throws (a declared codec key must resolve). */
function resolveKeyBytes(
  source: PayloadCodecKeySpec["value_from"],
  runtimePath: string,
  environment: Record<string, string | undefined>,
): Buffer {
  const { env, file } = source;
  if (env !== undefined) {
    const resolved = environment[env];
    const normalized = resolved?.trim();
    if (normalized === undefined || normalized === "") {
      throw new PayloadCodecError(
        resolved === undefined
          ? `missing required secret for ${runtimePath}: env ${env} is not set`
          : `secret for ${runtimePath} from env ${env} is empty`,
      );
    }
    return Buffer.from(normalized, "utf8");
  }
  if (file !== undefined) {
    const secretFile = expandHome(file);
    let raw: Buffer;
    try {
      raw = readFileSync(secretFile);
    } catch {
      throw new PayloadCodecError(`missing required secret for ${runtimePath}: file ${file} does not exist`);
    }
    if (raw.length === 0) throw new PayloadCodecError(`secret for ${runtimePath} from file ${file} is empty`);
    return raw;
  }
  throw new PayloadCodecError(`secret value_from for ${runtimePath} must configure env or file`);
}

/**
 * Build the codec from a resolved `runtime.temporal.payload_codec` block, or `undefined`
 * when absent. Fail-closed (D188-3): every declared key MUST resolve to exactly 32 bytes
 * or this throws — a PII codec never silently degrades to plaintext.
 */
export function buildPayloadCodec(
  spec: PayloadCodecSpec | undefined,
  environment: Record<string, string | undefined> = process.env,
): TypefluxAesGcmPayloadCodec | undefined {
  if (spec === undefined) return undefined;
  const keys = new Map<string, Buffer>();
  for (const key of spec.keys) {
    const runtimePath = `runtime.temporal.payload_codec.keys[${key.id}].value_from`;
    const resolved = resolveKeyBytes(key.value_from, runtimePath, environment);
    if (resolved.length !== KEY_LEN) {
      throw new PayloadCodecError(
        `payload codec key '${key.id}' must be exactly ${KEY_LEN} bytes (got ${resolved.length})`,
      );
    }
    keys.set(key.id, resolved);
  }
  return new TypefluxAesGcmPayloadCodec({ currentKid: spec.current, keys });
}
