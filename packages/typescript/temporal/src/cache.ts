/**
 * Cross-run cache key + record contract (#391), mirroring the Python
 * `contracts/cache.py`. The key a cross-run cache store, adopter cache implementations, and
 * this SDK all use to memoize an AI-activity result across runs.
 */

import { createHash } from "node:crypto";

import { canonicalJson } from "./canonical-json.js";

/** Identifies a cached AI-activity result. */
export interface CacheKey {
  /** The activity ("node") name. */
  activity: string;
  /** Digest of the call-determining inputs (see {@link cacheInputHash}). */
  input_hash: string;
  /** Tenancy/partition keys (e.g. `{ company_id, product_id }`); empty = global. */
  scope: Record<string, string>;
}

/** A stored cache entry: the cached validated output plus provenance. */
export interface CacheRecord {
  key: CacheKey;
  output: unknown;
  /** ISO-8601 timestamp. */
  created_at: string;
  /** Ties the entry to the output schema it was produced against. */
  output_schema_hash: string;
  manifest_hash?: string;
  tokens_saved?: number;
  /**
   * Subject id(s) whose data produced this entry (#715 slice 1) — so a later
   * per-subject cache invalidation can find it. Lives on the RECORD, NOT in the
   * {@link CacheKey}: folding it into the key would change the digest and
   * over-partition scope-keyed entries. Present-only (absent when empty), so
   * records for subject-free calls and every previously stored entry are
   * byte-unchanged and the key digest is untouched.
   */
  subjects?: string[];
}

function sha256Hex(value: string): string {
  return createHash("sha256").update(value, "utf-8").digest("hex");
}

/** Construct a {@link CacheKey} (scope defaults to global). */
export function cacheKey(
  activity: string,
  inputHash: string,
  scope: Record<string, string> = {},
): CacheKey {
  return { activity, input_hash: inputHash, scope: { ...scope } };
}

/**
 * `sha256` over the key's canonical JSON. `canonicalJson` sorts keys, so the
 * digest is independent of `scope` insertion order.
 */
export function cacheKeyDigest(key: CacheKey): string {
  return sha256Hex(
    canonicalJson({
      activity: key.activity,
      input_hash: key.input_hash,
      scope: { ...key.scope },
    }),
  );
}

/**
 * Digest of the inputs that determine an AI-activity call. `renderedMessagesHash`
 * (frozen by the execution-manifest contract, #390) encodes the prompt rendered
 * with the input, so identical input + prompt + behavior params yield the same
 * hash. Mirrors an adopter cache's input hash.
 *
 * `artifacts` is the resolved-artifact identity (#504) in the
 * `artifactGroupsCacheIdentity` shape (per artifact: group/index/source_kind/
 * kind/media_type/role/sha256/size_bytes with absent fields dropped, plus the
 * SOURCE — url/uri/provider+file_id — whenever no sha256 pins the bytes; empty
 * groups excluded). An artifact part RENDERS as only its group name + preamble
 * text, so the rendered-messages hash cannot see the underlying bytes — without
 * this fold, replacing a file (or the source location of an unhashed artifact)
 * under the same group name would serve a stale cached output. Omitted from the
 * payload when absent or empty (Python `if artifacts:` parity), so keys for
 * artifact-free calls (and all previously stored entries) are unchanged.
 */
export function cacheInputHash(params: {
  activity: string;
  inputSchemaHash: string;
  renderedMessagesHash: string;
  providerParams?: Record<string, unknown> | null;
  artifacts?: readonly Record<string, unknown>[] | null;
}): string {
  const payload: Record<string, unknown> = {
    activity: params.activity,
    input_schema_hash: params.inputSchemaHash,
    rendered_messages_hash: params.renderedMessagesHash,
    provider_params: params.providerParams ? { ...params.providerParams } : {},
  };
  if (params.artifacts != null && params.artifacts.length > 0) {
    payload["artifacts"] = params.artifacts.map((group) => ({ ...group }));
  }
  return sha256Hex(canonicalJson(payload));
}

/**
 * Build a {@link CacheRecord}. `manifestHash` / `tokensSaved` are optional
 * provenance and are omitted from the record when not provided (matching the
 * Python `cache_record`).
 */
export function cacheRecord(params: {
  key: CacheKey;
  output: unknown;
  createdAt: string;
  outputSchemaHash: string;
  manifestHash?: string | null;
  tokensSaved?: number | null;
  subjects?: readonly string[] | null;
}): CacheRecord {
  const record: CacheRecord = {
    key: {
      activity: params.key.activity,
      input_hash: params.key.input_hash,
      scope: { ...params.key.scope },
    },
    output: params.output,
    created_at: params.createdAt,
    output_schema_hash: params.outputSchemaHash,
  };
  if (params.manifestHash != null) {
    record.manifest_hash = params.manifestHash;
  }
  if (params.tokensSaved != null) {
    record.tokens_saved = params.tokensSaved;
  }
  if (params.subjects != null && params.subjects.length > 0) {
    record.subjects = [...params.subjects];
  }
  return record;
}
