/**
 * Activity execution: structured-call + validation-repair loop (the TS
 * counterpart to the Python executor). Runs a `defineActivity` descriptor against
 * a `ModelProvider`, validates the structured output with the Zod schema (and the
 * optional input-aware outputCheck, #745), repairs on failure, runs the context
 * hook (#397), and only then applies the cache policy (#398) — the cache is
 * written after full acceptance. The executor owns validation — the provider
 * returns the raw output.
 */

import { z } from "zod";

import type {
  ActivityContext,
  ActivityContextOverrides,
  ActivityDescriptor,
  AiActivityDescriptor,
  CodeActivityDescriptor,
  OutputCheckViolation,
} from "./activity.js";
import { buildActivityExecutionManifest, type Json, type ProviderParams } from "./manifest.js";
import { artifactGroupsSummary } from "./artifacts.js";
import {
  artifactGroupsCacheIdentity,
  attachArtifactMessages,
  type ArtifactInput,
  type ResolvedArtifactGroup,
} from "./artifacts.js";
import {
  type CacheKey,
  type CacheRecord,
  cacheInputHash,
  cacheKey,
  cacheKeyDigest,
  cacheRecord,
} from "./cache.js";
import { type ChatMessage, messagesHash } from "./manifest-hashing.js";
import { applyModeration, type ModerationPolicyBlock } from "./moderation.js";
import { type ActivityObservation, type ActivityObserver, NO_OP_OBSERVER } from "./observer.js";
import {
  providerCallWaitMetadata,
  providerPolicySelectionMetadata,
  type ProviderRateLimitController,
} from "./provider-limits.js";
import type { PromptRegistry } from "./prompt-registry.js";
import { renderMessages } from "./render.js";
import type { JsonSchema } from "./provider-schema.js";
// Runtime edge into session-cache.js (whose own value imports — render, canonical-json,
// zod — never reach back into execute.js, so no new module cycle forms).
import {
  assertStableSystemPrefix,
  noSessionCacheHandle,
  sessionCacheIdentity,
  sessionCacheStyleOf,
  supportsSessionCache,
  type CachedSessionHandle,
  type SessionCacheStyle,
} from "./session-cache.js";

/**
 * Token accounting for one provider call (Python `ProviderUsage`). An in-process
 * shape (camelCase, not wire data): providers report it through the `usageSink`
 * callback and the executor forwards it to the observer. The cache fields are
 * how a session-cache hit becomes OBSERVABLE (#478): `cacheReadTokens` counts
 * tokens served from a provider-side cache (a hit), `cacheWriteTokens` tokens
 * spent creating/refreshing one (a miss that primed the cache).
 */
export interface ProviderUsage {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  model?: string;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
}

/**
 * Python's `ProviderUsage.cache_hit` tri-state: true when tokens were served
 * from cache, false when only cache writes happened, undefined when the
 * provider reported no cache fields at all (no cache involvement to speak of).
 */
export function providerUsageCacheHit(usage: ProviderUsage): boolean | undefined {
  if (usage.cacheReadTokens === undefined && usage.cacheWriteTokens === undefined) {
    return undefined;
  }
  return (usage.cacheReadTokens ?? 0) > 0;
}

/**
 * Merge provider-params layers with Python's precedence
 * (`provider_default_params.merge(prompt_params, activity_params)`): later layers win
 * FIELD-WISE, and a `null`/`undefined` entry in a later layer does NOT erase an earlier
 * value (Python merges only non-None fields). Returns a fresh object.
 */
export function mergeProviderParams(
  ...layers: (Record<string, unknown> | null | undefined)[]
): Record<string, unknown> {
  const merged: Record<string, unknown> = {};
  for (const layer of layers) {
    if (layer == null) {
      continue;
    }
    for (const [key, value] of Object.entries(layer)) {
      if (value === undefined || value === null) {
        continue;
      }
      // Python's tuple-valued field (`stop`) defaults to () and its merge treats an
      // empty tuple as ABSENT — an empty list from a later layer must not erase an
      // earlier non-empty one (codex #495 PR-A1 review). Generalized to EVERY key
      // (unknown keys can't exist in Python's typed dataclass, so there is no
      // reference behavior to diverge from).
      if (Array.isArray(value) && value.length === 0) {
        continue;
      }
      merged[key] = value;
    }
  }
  return merged;
}

/**
 * Params that shape the call's TRANSPORT behavior, not the model output — excluded
 * from cross-run cache keys and session-cache identities (Python `behavior_dict`
 * with `include_operational=False`): a timeout change must not partition the cache.
 */
const OPERATIONAL_PROVIDER_PARAM_KEYS = new Set(["timeout"]);

/** The merged params minus operational fields — the shape cache keys and identities fold. */
export function behaviorProviderParams(params: Record<string, unknown>): Record<string, unknown> {
  const behavior: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (!OPERATIONAL_PROVIDER_PARAM_KEYS.has(key)) {
      behavior[key] = value;
    }
  }
  return behavior;
}

/** A finite-number behavior param, or undefined when absent/off-type (providers forward, never coerce). */
export function numberProviderParam(
  params: Record<string, unknown> | undefined,
  key: string,
): number | undefined {
  const value = params?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/**
 * The `stop` sequences param as a non-empty string array, or undefined. A bare
 * string coerces to a one-element list (Python `ProviderParams.from_mapping` parity).
 */
export function stopProviderParam(params: Record<string, unknown> | undefined): string[] | undefined {
  const value = params?.["stop"];
  if (typeof value === "string" && value !== "") {
    return [value];
  }
  return Array.isArray(value) && value.length > 0 && value.every((entry) => typeof entry === "string")
    ? (value as string[])
    : undefined;
}

export interface StructuredCallParams {
  messages: ChatMessage[];
  /** The provider-safe output schema, as the provider's response schema. */
  outputSchema: JsonSchema;
  model?: string;
  /**
   * The MERGED behavior params for this call (#495: call defaults < prompt < activity).
   * Providers map the common fields (temperature/max_tokens/top_p/top_k/stop/seed/
   * frequency_penalty/presence_penalty, Gemini thinking_budget) onto their requests
   * and SILENTLY ignore unknown or off-type values — the YAML spec layer validates
   * loudly at load (#495 A2); direct API callers get the forward-only stance.
   * `timeout` (seconds) is OPERATIONAL: it reaches the transport via
   * `callOptions().timeoutMs` and is excluded from cache keys/session identities.
   * The dedicated `model` field is authoritative for the model; a `model` key in
   * this record is the merge residue providers must not read.
   */
  providerParams?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  /**
   * Aborts the in-flight provider call when the activity is cancelled (#487). Providers forward
   * it to their transports; an abort-aware transport (fetch-based SDKs) then drops the request
   * instead of running it to completion.
   */
  signal?: AbortSignal;
  /**
   * Resolved artifact groups (#481): providers map `artifact`/`artifact_group` content parts to
   * native parts (image/document blocks) by looking up these groups. An artifact part whose group
   * is absent fails loud with a typed `ProviderConfigError`.
   */
  artifacts?: readonly ResolvedArtifactGroup[];
  /**
   * The provider-side cached session prepared before a map fan-out (#478). A
   * session-cache-capable provider uses it to skip re-priming the stable prefix
   * per item (mark prefix breakpoints / pass the provider-native `cache_id`);
   * providers without the capability ignore it. Threaded by the executor
   * (ladder PR3); consumed by the provider implementations (PR4/PR5).
   */
  cachedSession?: CachedSessionHandle;
  /**
   * Reports the call's token usage (#478): providers invoke it after the
   * response arrives — typically once; on multiple reports the last wins
   * (Python `reported_usage[-1]` parity). The executor supplies a sink that
   * records the usage on the generation observation and forwards it to the
   * caller's `ExecuteActivityOptions.usageSink`.
   */
  usageSink?: (usage: ProviderUsage) => void;
}

/** Options a provider forwards to its transport call — currently just the abort signal (#487). */
export interface TransportCallOptions {
  signal?: AbortSignal;
  /**
   * Per-call transport timeout in MILLISECONDS (#495: the params `timeout` seconds
   * ×1000 — an OPERATIONAL param, excluded from cache keys). Adapters map it to
   * their SDK's per-request timeout (e.g. `{ timeout: timeoutMs }` for the OpenAI/
   * Anthropic clients).
   */
  timeoutMs?: number;
}

/**
 * The transport call options for a structured call, or `undefined` when there is nothing to
 * forward (so a single-argument fake/adapter transport sees the same call shape as before, and no
 * `{ signal: undefined }` is materialized under `exactOptionalPropertyTypes`).
 */
export function callOptions(params: StructuredCallParams): TransportCallOptions | undefined {
  const timeoutSeconds = numberProviderParam(params.providerParams, "timeout");
  const options: TransportCallOptions = {
    ...(params.signal !== undefined ? { signal: params.signal } : {}),
    ...(timeoutSeconds !== undefined && timeoutSeconds > 0 ? { timeoutMs: timeoutSeconds * 1000 } : {}),
  };
  return Object.keys(options).length > 0 ? options : undefined;
}

/**
 * The stable prefix a session-cache-capable provider is asked to prepare
 * (Python `prepare_cached_session`'s signature): the system/prefix messages,
 * the activity's `cache: reference` artifact groups, the resolved model/params,
 * and the precomputed identity hash + optional TTL to record on the handle.
 */
export interface PrepareCachedSessionParams {
  messages: ChatMessage[];
  artifacts: readonly ResolvedArtifactGroup[];
  model?: string;
  providerParams?: Record<string, unknown>;
  identityHash: string;
  ttlSeconds?: number;
}

/**
 * A structured model caller — returns the RAW structured output (the executor validates).
 *
 * The session-cache members (#478) are optional and structural (Python's
 * getattr-probed `supports_session_cache`): a provider opts in by declaring
 * `supportsSessionCache: true` + `sessionCacheStyle` and implementing
 * `prepareCachedSession` (called once by the cache-prep activity before a map
 * fan-out) and, for reference-style caches, `releaseCachedSession` (best-effort
 * cleanup after it). Providers without them are simply never asked — the
 * executor fails soft to full-context calls.
 */
export interface ModelProvider {
  structuredCall(params: StructuredCallParams): unknown | Promise<unknown>;
  /**
   * A stable name for manifests/policies/cache identity (Python `provider_name`).
   * `providerIdentifier` falls back to the kebab-cased class name when absent.
   */
  providerName?: string;
  supportsSessionCache?: boolean;
  sessionCacheStyle?: SessionCacheStyle;
  prepareCachedSession?(params: PrepareCachedSessionParams): CachedSessionHandle | Promise<CachedSessionHandle>;
  releaseCachedSession?(handle: CachedSessionHandle): void | Promise<void>;
}

/** Cross-run cache store keyed by the #391 cache contract (parity with Python #398). */
export interface CacheStore {
  get(key: CacheKey): CacheRecord | undefined | Promise<CacheRecord | undefined>;
  set(key: CacheKey, record: CacheRecord): void | Promise<void>;
}

/**
 * The honest coverage boundary of a subject-scoped cache erasure (#715 slice 3).
 * The write-time index only sees records that carried a `subjects` field when they
 * were written (slice 1 made that field real). Entries written BEFORE subject
 * plumbing, or by a workflow that touched this subject's data through a path that
 * did not declare the subject, carry no subject and are invisible here; erasing them
 * requires a full store flush. Mirrors the design's audit-honesty requirement.
 */
export const CACHE_ERASURE_COVERAGE_CAVEAT =
  "cache erasure only covers records written with a subject index (#715 slice 1+): " +
  "entries written before subject plumbing, or by workflows that touched this " +
  "subject's data through an un-declared path, carry no subject and are invisible " +
  "to a per-subject erase. A full store flush is the only way to guarantee their " +
  "removal.";

/**
 * Result of a subject-scoped cache erasure (#715 slice 3). Store-level primitive
 * report; the slice-5 erasure receipt folds it into the cross-surface audit record.
 * Carries ids/counts only — `keyDigests` are opaque sha256 cache-key hashes (NOT
 * sensitive, so included for audit), never the cached output or subject PII.
 * `dryRun` reports what WOULD be deleted without mutating (`keysDeleted` is 0);
 * `supported: false` names the `fullFlushFallback` when the store lacks the
 * capability. `warnings` always carries {@link CACHE_ERASURE_COVERAGE_CAVEAT}.
 */
export interface SubjectCacheErasureReport {
  subjectId: string;
  dryRun: boolean;
  storeClass: string;
  supported: boolean;
  keysFound: number;
  keysDeleted: number;
  keyDigests: string[];
  failures: string[];
  warnings: string[];
  fullFlushFallback?: string;
}

/**
 * Optional {@link CacheStore} capability: per-subject invalidation (#715 slice 3).
 * A store opts in by implementing `eraseSubject` and maintaining a subject→digest
 * index at write time from each record's `subjects` field. Duck-detected via
 * {@link eraseSubjectFromCache}, which returns a not-supported report naming the
 * store class + the full-flush fallback for a plain store. `dryRun: true` reports
 * the affected keys without deleting; `dryRun: false` deletes and reports.
 *
 * Atomicity contract: implementors must make `eraseSubject` atomic with respect to
 * `set` — both are compound operations over the records and the subject index, and
 * an erase interleaved with an overwrite of the same key can otherwise delete a
 * fresh record belonging to a DIFFERENT subject while reporting a clean erase. A
 * synchronous store gets this for free from the single-threaded runtime; an async
 * (remote-backed) store must serialize the two (e.g. a transaction or mutex).
 */
export interface SubjectErasableCacheStore extends CacheStore {
  eraseSubject(
    subjectId: string,
    opts: { dryRun: boolean },
  ): SubjectCacheErasureReport | Promise<SubjectCacheErasureReport>;
}

/** A clear not-supported outcome naming the store class + the flush fallback. */
export function notSupportedCacheErasureReport(
  store: object,
  subjectId: string,
  opts: { dryRun: boolean },
): SubjectCacheErasureReport {
  const storeClass = store?.constructor?.name ?? "CacheStore";
  return {
    subjectId,
    dryRun: opts.dryRun,
    storeClass,
    supported: false,
    keysFound: 0,
    keysDeleted: 0,
    keyDigests: [],
    failures: [],
    warnings: [CACHE_ERASURE_COVERAGE_CAVEAT],
    fullFlushFallback:
      `${storeClass} has no subject index (does not implement ` +
      "SubjectErasableCacheStore); per-subject cache invalidation is unavailable. " +
      "Flush the whole store to erase this subject's entries, or accept " +
      "stale-but-inert cached outputs.",
  };
}

/**
 * True when `store` implements the {@link SubjectErasableCacheStore} capability.
 * Checks all three methods — `eraseSubject` alone must not narrow an object that
 * lacks the base `get`/`set` surface to a full store (it would crash later).
 */
export function isSubjectErasableCacheStore(store: unknown): store is SubjectErasableCacheStore {
  if (typeof store !== "object" || store === null) {
    return false;
  }
  const candidate = store as { get?: unknown; set?: unknown; eraseSubject?: unknown };
  return (
    typeof candidate.get === "function" &&
    typeof candidate.set === "function" &&
    typeof candidate.eraseSubject === "function"
  );
}

/**
 * Erase a subject's cache entries if the store supports it; else report
 * not-supported. Capability-detects {@link SubjectErasableCacheStore} and delegates,
 * or returns {@link notSupportedCacheErasureReport} naming the store class + the
 * documented full-flush fallback for a plain store.
 */
export function eraseSubjectFromCache(
  store: object,
  subjectId: string,
  opts: { dryRun: boolean },
): SubjectCacheErasureReport | Promise<SubjectCacheErasureReport> {
  if (isSubjectErasableCacheStore(store)) {
    return store.eraseSubject(subjectId, opts);
  }
  return notSupportedCacheErasureReport(store, subjectId, opts);
}

/**
 * Process-local reference cache store (a Map keyed by the flat key digest).
 *
 * Implements the {@link SubjectErasableCacheStore} capability via a write-time
 * subject→digest index (#715 slice 3): `set` indexes each record under every id in
 * its `subjects` field, re-indexing on overwrite so a stale subject entry can never
 * leak, and `eraseSubject` consults the index for dry-run/execute erasure. Records
 * without `subjects` are never indexed — correct: they carry no subject data, so a
 * per-subject erase leaves them untouched.
 *
 * Atomicity invariant: `set` and `eraseSubject` are fully synchronous — no `await`
 * anywhere inside either — so each compound section (unindex-old → write → reindex;
 * read-index → delete → prune) runs to completion without interleaving on the
 * single-threaded runtime. Adding an `await` inside either method WOULD break this
 * (the Python edition needs an explicit lock for the same reason); if one becomes
 * necessary, add a mutex across each whole operation.
 */
export class InMemoryCacheStore implements SubjectErasableCacheStore {
  private readonly records = new Map<string, CacheRecord>();
  /** subject id -> set of key digests written under that subject. */
  private readonly subjectIndex = new Map<string, Set<string>>();

  get(key: CacheKey): CacheRecord | undefined {
    return this.records.get(cacheKeyDigest(key));
  }

  set(key: CacheKey, record: CacheRecord): void {
    const digest = cacheKeyDigest(key);
    // Overwrite: drop the prior record's subject entries first, so re-writing a key
    // under a different subject set never leaves a stale index pointer.
    const prior = this.records.get(digest);
    if (prior !== undefined) {
      this.unindex(digest, prior.subjects ?? []);
    }
    this.records.set(digest, record);
    for (const subjectId of record.subjects ?? []) {
      let keys = this.subjectIndex.get(subjectId);
      if (keys === undefined) {
        keys = new Set<string>();
        this.subjectIndex.set(subjectId, keys);
      }
      keys.add(digest);
    }
  }

  eraseSubject(subjectId: string, opts: { dryRun: boolean }): SubjectCacheErasureReport {
    const digests = [...(this.subjectIndex.get(subjectId) ?? new Set<string>())].sort();
    const storeClass = this.constructor.name;
    if (opts.dryRun) {
      return {
        subjectId,
        dryRun: true,
        storeClass,
        supported: true,
        keysFound: digests.length,
        keysDeleted: 0,
        keyDigests: digests,
        failures: [],
        warnings: [CACHE_ERASURE_COVERAGE_CAVEAT],
      };
    }
    let deleted = 0;
    for (const digest of digests) {
      const record = this.records.get(digest);
      if (record === undefined) {
        continue;
      }
      this.records.delete(digest);
      deleted += 1;
      // A record may carry several subjects; deleting it erases the shared entry, so
      // prune the digest from EVERY subject it was indexed under (not just this one).
      this.unindex(digest, record.subjects ?? []);
    }
    return {
      subjectId,
      dryRun: false,
      storeClass,
      supported: true,
      keysFound: digests.length,
      keysDeleted: deleted,
      keyDigests: digests,
      failures: [],
      warnings: [CACHE_ERASURE_COVERAGE_CAVEAT],
    };
  }

  /** Remove `digest` from each subject's index set, dropping emptied sets. */
  private unindex(digest: string, subjects: readonly string[]): void {
    for (const subjectId of subjects) {
      const keys = this.subjectIndex.get(subjectId);
      if (keys === undefined) {
        continue;
      }
      keys.delete(digest);
      if (keys.size === 0) {
        this.subjectIndex.delete(subjectId);
      }
    }
  }
}

/**
 * Terminal output-validation failure after exhausting the repair retries. The
 * terminal cause is EITHER a schema-parse miss (`zodError`) or an input-aware
 * {@link OutputCheckViolation} list (#745) — exactly one is set. Only the
 * outputCheck violations (author-written) are named in the message; a raw
 * `zodError` is withheld so model-output previews never reach Temporal history.
 */
export class ActivityValidationError extends Error {
  readonly activityName: string;
  readonly attempts: number;
  /** The zod parse error, when the terminal failure was a schema-parse miss. */
  readonly zodError?: z.ZodError;
  /** The outputCheck violations, when the terminal failure was a cross-field check (#745). */
  readonly outputCheckViolations?: OutputCheckViolation[];

  constructor(activityName: string, attempts: number, detail: z.ZodError | OutputCheckViolation[]) {
    const isViolations = Array.isArray(detail);
    super(
      `output validation failed for activity ${JSON.stringify(activityName)} ` +
        `after ${attempts} attempt(s)` +
        (isViolations ? `: outputCheck violations: ${renderOutputCheckViolations(detail)}` : ""),
    );
    this.name = "ActivityValidationError";
    this.activityName = activityName;
    this.attempts = attempts;
    if (isViolations) {
      this.outputCheckViolations = detail;
    } else {
      this.zodError = detail;
    }
  }
}

/**
 * Render an {@link OutputCheckViolation} list into a legible body for the repair
 * prompt and the terminal error (#745), mirroring how a schema-parse miss serializes
 * its zod issues: one line per violation, `path: message` when a path is present.
 */
function renderOutputCheckViolations(violations: readonly OutputCheckViolation[]): string {
  return violations
    .map((violation) => {
      const path = violation.path !== undefined && violation.path.length > 0 ? violation.path.join(".") : "";
      return path !== "" ? `- ${path}: ${violation.message}` : `- ${violation.message}`;
    })
    .join("\n");
}

/**
 * Run the descriptor's optional {@link ActivityDescriptor.outputCheck} (#745). Returns a
 * non-empty violations list when the check REJECTS (a throw is normalized to a single
 * violation carrying its message), or `undefined` when it passes / no check is defined.
 * The `input` MUST be the parsed input so defaults are materialized (design contract).
 */
function runOutputCheck<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  input: z.infer<In>,
  output: z.infer<Out>,
): OutputCheckViolation[] | undefined {
  if (descriptor.outputCheck === undefined) {
    return undefined;
  }
  let result: unknown;
  try {
    result = descriptor.outputCheck(input, output);
  } catch (error) {
    // A throw is the ergonomic "reject" signal (e.g. an assertion helper); normalize
    // it to a single violation so both throw and returned-list flow the same repair path.
    return [{ message: error instanceof Error ? error.message : String(error) }];
  }
  // The normalization runs OUTSIDE the try: a contract-shape TypeError below is an
  // author bug and must fail loud, never be swallowed into a "violation" repair turn.
  return normalizeOutputCheckResult(result);
}

/** Structural probe for a single {@link OutputCheckViolation} (a string `message`). */
function isOutputCheckViolation(value: unknown): value is OutputCheckViolation {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    typeof (value as { message?: unknown }).message === "string"
  );
}

/**
 * Normalize an outputCheck's return value (#745 review): `undefined`/`null`/`[]` is
 * a PASS; an array of violations rejects; a SINGLE violation object (a common slip
 * for `[violation]`) is coerced to a one-element list; anything else — a string, a
 * number, truthy junk — is a pointed TypeError naming the contract, because
 * silently treating it as pass (or as violations) fails open/garbled.
 */
function normalizeOutputCheckResult(result: unknown): OutputCheckViolation[] | undefined {
  if (result === undefined || result === null) {
    return undefined;
  }
  if (Array.isArray(result)) {
    if (result.length === 0) {
      return undefined;
    }
    if (!result.every(isOutputCheckViolation)) {
      throw new TypeError("outputCheck must return void or OutputCheckViolation[]");
    }
    return result;
  }
  if (isOutputCheckViolation(result)) {
    return [result];
  }
  throw new TypeError("outputCheck must return void or OutputCheckViolation[]");
}

/**
 * A retryable provider failure (rate limit, network blip, 5xx). When
 * `executeActivity`'s `transientRetries` is set, the provider call is retried on
 * this (or whatever `isTransientError` classifies). Inside a Temporal worker,
 * prefer Temporal's `RetryPolicy` at the activity boundary; this opt-in inner
 * retry is for direct/non-worker use and for absorbing fast blips.
 */
export class ProviderTransientError extends Error {
  /**
   * Parsed Retry-After hint, in seconds; undefined when the response carried none.
   * Any transient response may carry one (a 503 "overloaded" as much as a 429 —
   * Python attaches it at every transient wrap site), and the retry loop FLOORS
   * the computed backoff with it.
   */
  readonly retryAfterSeconds: number | undefined;

  constructor(message: string, options?: { retryAfterSeconds?: number; cause?: unknown }) {
    super(message);
    this.name = "ProviderTransientError";
    this.retryAfterSeconds = options?.retryAfterSeconds;
    if (options?.cause !== undefined) {
      (this as { cause?: unknown }).cause = options.cause;
    }
  }
}

/**
 * A provider rate limit — 429 or an SDK `RateLimitError` (#529; Python
 * `ProviderRateLimitError` parity). A `ProviderTransientError` subclass, so every
 * existing classifier keeps retrying it; the distinct class lets retry policy
 * select it (`retryRateLimits` vs `retryTransientErrors`).
 */
export class ProviderRateLimitError extends ProviderTransientError {
  constructor(message: string, options?: { retryAfterSeconds?: number; cause?: unknown }) {
    super(message, options);
    this.name = "ProviderRateLimitError";
  }
}

/**
 * Structural Retry-After probe for a thrown transport error (Python
 * `_shared.retry_after_seconds` parity): a numeric `retryAfter`/`retryAfterSeconds`
 * attribute wins (SDK-shaped errors), else the `retry-after` header on
 * `error.response.headers` (a `Headers`-like `.get()` or a plain record).
 * Negative/unparsable values are treated as absent.
 */
export function retryAfterSecondsFrom(error: unknown): number | undefined {
  const record = error as Record<string, unknown> | null;
  for (const attr of ["retryAfter", "retryAfterSeconds"]) {
    const value = record?.[attr];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      return value;
    }
  }
  // The OpenAI/Anthropic JS SDKs expose response headers TOP-LEVEL on the error
  // (`APIError.headers`) — probe that alongside `error.response.headers` (the
  // fetch-style shape, and what Python's httpx errors carry).
  const sources = [record?.headers, (record?.response as { headers?: unknown } | undefined)?.headers];
  for (const headers of sources) {
    let raw: unknown;
    try {
      if (typeof (headers as { get?: unknown } | null)?.get === "function") {
        raw = (headers as { get: (name: string) => unknown }).get("retry-after");
      } else if (headers !== null && typeof headers === "object") {
        // A plain record has no case-insensitive lookup — accept the two real spellings.
        const plain = headers as Record<string, unknown>;
        raw = plain["retry-after"] ?? plain["Retry-After"];
      }
    } catch {
      continue; // defensive against exotic header types (Python parity)
    }
    let seconds: number;
    if (typeof raw === "number") {
      seconds = raw;
    } else if (typeof raw === "string") {
      // Python float() semantics (minus inf/nan): decimal or scientific only.
      // A bare Number() would coerce ""/whitespace to 0 and accept hex.
      if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(raw.trim())) {
        continue;
      }
      seconds = Number(raw.trim());
    } else {
      continue;
    }
    if (!Number.isFinite(seconds) || seconds < 0) {
      continue;
    }
    return seconds;
  }
  return undefined;
}

/**
 * A referenced provider-side session cache is gone (expired or deleted) — Python's
 * `ProviderCacheUnavailableError` (#368). Thrown by a reference-style provider when a
 * per-item call fails BECAUSE of its `cached_content` reference. The map wrapper (#478
 * PR6) recovers by re-running the item once WITHOUT the handle: the per-item messages
 * were built without the reference artifacts (they lived in the cache), so an in-place
 * retry with the same handle would fail identically.
 */
export class ProviderCacheUnavailableError extends Error {
  readonly provider: string;
  readonly statusCode: number | undefined;

  constructor(message: string, options: { provider: string; statusCode?: number; cause?: unknown }) {
    super(message);
    this.name = "ProviderCacheUnavailableError";
    this.provider = options.provider;
    this.statusCode = options.statusCode;
    if (options.cause !== undefined) {
      (this as { cause?: unknown }).cause = options.cause;
    }
  }
}

/**
 * A non-retryable provider configuration/request-construction failure (parity with Python's
 * `ProviderConfigError`): an unsupported message role, a content part the provider can't
 * represent, a provider extension addressed elsewhere, a missing model, an invalid constructor
 * option — and token-limit truncation (#784), where the configured limit deterministically
 * truncates the identical call on every retry. Sampling-dependent response-shape failures
 * (unparsable JSON, missing content) stay plain errors — a retry can genuinely succeed there.
 */
export class ProviderConfigError extends Error {
  /** The provider that rejected the configuration (e.g. `"gemini"`). */
  readonly provider: string | undefined;

  constructor(message: string, options?: { provider?: string; cause?: unknown }) {
    super(message);
    this.name = "ProviderConfigError";
    this.provider = options?.provider;
    if (options?.cause !== undefined) {
      (this as { cause?: unknown }).cause = options.cause;
    }
  }
}

/**
 * Fail loud on a response cut off at the output-token cap (#784; parity with Python's
 * `raise_if_truncated` in `providers/_shared.py`). Truncation is deterministic — the same
 * limit truncates the identical call on every retry — so it is a non-retryable
 * `ProviderConfigError`, never a plain (retryable) `Error`. Centralized so the providers
 * cannot drift on the classification; each keeps its own message naming the vendor param.
 */
export function raiseIfTruncated(truncated: boolean, options: { provider: string; message: string }): void {
  if (!truncated) {
    return;
  }
  throw new ProviderConfigError(options.message, { provider: options.provider });
}

/**
 * A non-retryable provider POLICY violation raised at the per-call runtime guard
 * (#454; parity with Python's `ProviderPolicyError`): the model a call actually
 * resolved to is not allowed by the selected project policy. Terminal — the same
 * call reproduces it — so a worker treats it as non-retryable. The originating
 * policy error (if any) is carried as `cause`.
 */
export class ProviderPolicyError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message);
    this.name = "ProviderPolicyError";
    if (options?.cause !== undefined) {
      (this as { cause?: unknown }).cause = options.cause;
    }
  }
}

/**
 * A per-call runtime guard (#454; Python `ProviderModelPolicyGuard`): invoked with
 * the FULLY-RESOLVED model a call will use (after prompt/activity overrides), so a
 * dynamic model an admission-time check could not see is still enforced. Throws to
 * block the call (the executor normalizes the throw to {@link ProviderPolicyError}).
 */
export type ProviderModelGuard = (call: ResolvedProviderCall) => void;

/** The resolved identity of a provider call handed to a {@link ProviderModelGuard}. */
export interface ResolvedProviderCall {
  providerName: string;
  model: string | undefined;
  activityName: string;
  promptName: string | undefined;
}

/**
 * Run a {@link ProviderModelGuard} (if any) and normalize any throw to the terminal
 * {@link ProviderPolicyError} (the worker marks it non-retryable). Shared by the
 * per-item executor and the session-cache prep, so BOTH provider calls are guarded.
 */
function invokeProviderModelGuard(guard: ProviderModelGuard | undefined, call: ResolvedProviderCall): void {
  if (guard === undefined) return;
  try {
    guard(call);
  } catch (error) {
    throw error instanceof ProviderPolicyError
      ? error
      : new ProviderPolicyError(error instanceof Error ? error.message : String(error), { cause: error });
  }
}

/**
 * Raised at a cooperative checkpoint when the activity's `cancellationSignal` has aborted
 * (parity with Python's `ActivityCancelled`, #487). Control flow, not a provider failure: it is
 * never retried, never wrapped as a terminal failure, and inside a Temporal worker the boundary
 * rethrows its `cause` (Temporal's `CancelledFailure`, the signal's reason) so the activity is
 * recorded as cancelled rather than failed.
 */
export class ActivityCancelledError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message);
    this.name = "ActivityCancelledError";
    if (options?.cause !== undefined) {
      (this as { cause?: unknown }).cause = options.cause;
    }
  }
}

/** Throw {@link ActivityCancelledError} if the activity's cancellation signal has aborted. */
function throwIfCancelled(signal: AbortSignal | undefined): void {
  if (signal?.aborted === true) {
    throw new ActivityCancelledError("activity cancelled", { cause: signal.reason });
  }
}

/**
 * A `setTimeout` delay that ends early when the signal aborts (the caller's post-delay
 * checkpoint then raises the typed error) — a cancel during a long retry backoff must not wait
 * out the sleep (parity with Python's asyncio.sleep being interrupted by task cancellation).
 * The abort listener is removed either way, so repeated backoffs never accumulate listeners.
 */
export function abortableDelay(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted === true) {
      resolve();
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface ExecuteActivityOptions {
  provider: ModelProvider;
  /**
   * The prompt messages. Supply these to pre-render yourself, OR omit them and
   * supply a `registry` to have the activity's `prompt` ref resolved + rendered
   * against the input (parity with Python `execute_ai_activity(registry=...)`).
   * One of `messages` or `registry` is required.
   */
  messages?: ChatMessage[];
  /** Resolves the activity's `prompt` ref when `messages` is omitted. */
  registry?: PromptRegistry;
  deps?: unknown;
  tenant?: Record<string, string>;
  /**
   * Runtime context fields that enrich the hook's `ActivityContext` — e.g. a
   * Temporal worker mapping the activity `Info` (workflowId/runId/attempt/…). Merged
   * over `activityName`/`tenant`/`deps`, which it cannot override.
   */
  context?: ActivityContextOverrides;
  cacheStore?: CacheStore;
  /** Observes the execution (activity span + per-attempt generations). Defaults to no-op. */
  observer?: ActivityObserver;
  model?: string;
  providerParams?: Record<string, unknown>;
  /**
   * Bounded retry of the provider call on a transient error (default `0` = off, so
   * inside a Temporal worker its `RetryPolicy` stays the retry mechanism). Each
   * output-validation attempt gets its own transient budget, so the worst-case
   * provider-call count is `(validationRetries + 1) * (transientRetries + 1)`.
   */
  transientRetries?: number;
  /**
   * Cooperative cancellation (#487): checked at the executor's checkpoints (entry/cache path,
   * each validation attempt, each transient-retry attempt and its backoff) and forwarded to the
   * provider call so an abort-aware transport drops the in-flight request. Inside a Temporal
   * worker this is `Context.current().cancellationSignal` (injected by the worker package); note
   * Temporal only DELIVERS cancellation to an activity that heartbeats (set a heartbeat timeout).
   */
  cancellationSignal?: AbortSignal;
  /**
   * Pre-resolved artifact groups (#481) forwarded to the provider call so `artifact`/
   * `artifact_group` content parts resolve to native parts. Wins over `artifactResolver`.
   */
  artifacts?: readonly ResolvedArtifactGroup[];
  /**
   * Resolves the descriptor's declared `artifacts` against the input per call (#481) — inject the
   * worker package's `artifactInputResolver(policy)`. A descriptor that declares artifacts with
   * neither `artifacts` nor a resolver supplied fails loud (they would silently vanish).
   */
  artifactResolver?: (
    inputValue: unknown,
    artifactInputs: readonly ArtifactInput[],
  ) => Promise<readonly ResolvedArtifactGroup[]> | readonly ResolvedArtifactGroup[];
  /** Classifies a thrown error as transient/retryable (default: `ProviderTransientError`). */
  isTransientError?: (err: unknown) => boolean;
  /** Delay between transient retries, in milliseconds (default `0` = immediate). */
  transientRetryDelayMs?: number;
  /**
   * Exponential backoff for transient retries (#495; Python `ProviderRetrySpec`):
   * `delay(attempt) = min(initialMs * multiplier^attempt, maxMs) * (1 + jitterRatio * rand)`
   * — jitter is POSITIVE-ONLY (the configured backoff is a floor; Python parity).
   * Wins over the fixed `transientRetryDelayMs` when present. Jitter uses
   * `Math.random()` — activities run OUTSIDE the workflow sandbox, so nondeterminism
   * here is fine (only workflow code must replay deterministically).
   */
  transientBackoff?: {
    initialMs: number;
    multiplier?: number;
    maxMs?: number;
    jitterRatio?: number;
  };
  /**
   * Per-provider/per-model call admission (#529 PR B; Python `provider_limits`):
   * `maxConcurrent` + `minIntervalSeconds` selected model > provider > default,
   * one SHARED limiter per policy key. Share one controller across every
   * activity registration in a worker — that sharing is what makes the limits
   * hold across concurrent activities.
   */
  providerLimitController?: ProviderRateLimitController;
  /**
   * Whether the transient-retry loop retries `ProviderRateLimitError` (#529; Python
   * `ProviderRetryPolicy.retry_rate_limits`). Default `true`. `false` re-throws a
   * rate-limit error immediately — plain transient errors are unaffected.
   */
  retryRateLimits?: boolean;
  /**
   * Whether the transient-retry loop retries NON-rate-limit transient errors (#529;
   * Python `retry_transient_errors`). Default `true`. `false` re-throws them
   * immediately — `ProviderRateLimitError` is selected by `retryRateLimits` alone.
   */
  retryTransientErrors?: boolean;
  /**
   * Receives each provider call's reported token usage (#478). The executor also
   * records every reported usage on the generation observation, so an observer
   * sees it without wiring a sink.
   */
  usageSink?: (usage: ProviderUsage) => void;
  /**
   * The provider-side cached session prepared before a map fan-out (#478, from
   * `prepareSessionCache`). When present: `cache_role: "reference"` artifacts skip
   * per-item resolution/attachment iff the handle's `reference_cached` is true,
   * the handle's identity folds into the cross-run cache key as
   * `__session_identity`, and the handle is forwarded to the provider call.
   */
  cachedSession?: CachedSessionHandle;
  /**
   * Per-call model policy guard (#454): invoked with the fully-resolved model
   * BEFORE the provider call (and before a cache hit is served), so a model an
   * admission-time check could not see is still enforced. A throw blocks the call —
   * the executor normalizes it to {@link ProviderPolicyError} (terminal).
   */
  providerModelGuard?: ProviderModelGuard;
  /**
   * Moderation verdict-escalation guard (#454): applied to the moderator's reported
   * categories/score at the output checkpoint; returns a block reason to force a
   * block (a policy can only tighten). See {@link ModerationPolicyBlock}.
   */
  moderationPolicyBlock?: ModerationPolicyBlock;
}

/**
 * The delay before retry number `attempt` (0-based): fixed, or exponential with jitter (#495).
 * The error's Retry-After hint FLOORS the delay AFTER the cap (#529; Python
 * `retry_delay_seconds`): the hint is server truth, so `maxMs` never caps it down, and
 * jitter extends the floored delay so synchronized workers still de-synchronize.
 */
function transientRetryDelay(
  options: ExecuteActivityOptions,
  attempt: number,
  retryAfterSeconds?: number,
): number {
  const retryAfterMs = retryAfterSeconds !== undefined ? retryAfterSeconds * 1000 : 0;
  const backoff = options.transientBackoff;
  if (backoff === undefined) {
    return Math.max(options.transientRetryDelayMs ?? 0, Math.round(retryAfterMs));
  }
  const multiplier = backoff.multiplier ?? 2;
  const capped = Math.min(backoff.initialMs * multiplier ** attempt, backoff.maxMs ?? Number.POSITIVE_INFINITY);
  const raw = Math.max(capped, retryAfterMs);
  const jitterRatio = backoff.jitterRatio ?? 0;
  // POSITIVE-ONLY jitter (Python `delay += delay * uniform(0, jitter_ratio)`): the
  // configured backoff is a FLOOR — jitter only extends it, never retries sooner
  // against a shared rate limit. Activities run OUTSIDE the workflow sandbox, so
  // Math.random here is fine.
  const jittered = jitterRatio > 0 ? raw * (1 + jitterRatio * Math.random()) : raw;
  return Math.max(0, Math.round(jittered));
}

/** Run `call`, retrying on a transient error up to `options.transientRetries` times. */
async function callWithTransientRetry(
  call: () => unknown | Promise<unknown>,
  options: ExecuteActivityOptions,
): Promise<unknown> {
  const maxRetries = options.transientRetries ?? 0;
  const isTransient =
    options.isTransientError ?? ((err: unknown) => err instanceof ProviderTransientError);
  for (let attempt = 0; ; attempt += 1) {
    throwIfCancelled(options.cancellationSignal);
    try {
      return await call();
    } catch (err) {
      // Cancellation wins over whatever shape the abort surfaced as (an abort-aware transport
      // may throw its own AbortError) — classify it before the transient logic sees it.
      throwIfCancelled(options.cancellationSignal);
      let transient: boolean;
      try {
        transient = isTransient(err);
      } catch {
        throw err; // a misbehaving classifier must not mask the provider error
      }
      if (attempt >= maxRetries || !transient) {
        throw err;
      }
      // Class selection (#529; Python `_should_retry_provider`): a rate-limit error is
      // governed by `retryRateLimits` ALONE, every other transient by `retryTransientErrors`.
      const rateLimited = err instanceof ProviderRateLimitError;
      if (rateLimited ? !(options.retryRateLimits ?? true) : !(options.retryTransientErrors ?? true)) {
        throw err;
      }
      // The Retry-After hint floors the backoff for EVERY transient class (Python
      // forwards exc.retry_after_seconds unconditionally) — a 503 can carry one too.
      const hint = err instanceof ProviderTransientError ? err.retryAfterSeconds : undefined;
      const delayMs = transientRetryDelay(options, attempt, hint);
      if (delayMs > 0) {
        await abortableDelay(delayMs, options.cancellationSignal);
        // A cancel that landed during the backoff aborts now — the delay itself ended early on
        // the abort, so this fires promptly rather than after the full sleep (parity with
        // Python's post-sleep raise_if_cancelled + asyncio's interrupted sleep).
        throwIfCancelled(options.cancellationSignal);
      }
    }
  }
}

/** Peel `.optional()` / `.nullable()` / `.default()` wrappers to the inner type. */
function innerType(schema: z.ZodType): z.ZodType {
  let current = schema;
  for (let depth = 0; depth < 8; depth += 1) {
    if (
      current instanceof z.ZodOptional ||
      current instanceof z.ZodNullable ||
      current instanceof z.ZodDefault
    ) {
      current = current.unwrap() as z.ZodType;
    } else {
      break;
    }
  }
  return current;
}

/**
 * Convert a provider `null` to absent for fields the Zod schema treats as
 * **optional-but-not-nullable** (#427), recursing through nested objects and
 * arrays. The provider-safe schema makes optionals required+nullable, so a model
 * emits `null`; Zod's `.optional()` (`T | undefined`) rejects it. This
 * selectively drops such nulls while leaving `.nullable()` / `.nullish()` nulls
 * untouched (those legitimately accept null).
 */
function coerceOptionalNulls(schema: z.ZodType, raw: unknown): unknown {
  const inner = innerType(schema);
  if (inner instanceof z.ZodArray && Array.isArray(raw)) {
    return raw.map((item) => coerceOptionalNulls(inner.element as z.ZodType, item));
  }
  if (!(inner instanceof z.ZodObject) || raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return raw;
  }
  const shape = inner.shape as Record<string, z.ZodType>;
  const out: Record<string, unknown> = { ...(raw as Record<string, unknown>) };
  for (const [key, field] of Object.entries(shape)) {
    if (!(key in out)) {
      continue;
    }
    if (out[key] === null && !field.safeParse(null).success && field.safeParse(undefined).success) {
      delete out[key];
    } else {
      out[key] = coerceOptionalNulls(field, out[key]);
    }
  }
  return out;
}

async function runHook<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  input: z.infer<In>,
  output: z.infer<Out>,
  ctx: ActivityContext,
): Promise<z.infer<Out>> {
  if (descriptor.hook === undefined) {
    return output;
  }
  const hooked = await descriptor.hook(input, output, ctx);
  // Re-validate the hook's output so the result is always schema-valid, matching
  // the Python executor's post-hook type check.
  return descriptor.output.parse(hooked) as z.infer<Out>;
}

/**
 * Produce the final returned output: run the hook, then the moderation checkpoint
 * (#453) over the hook's output. Used on BOTH the fresh and cache-hit paths so a
 * blocked output stays blocked even when served from cache (validate -> hook ->
 * moderate, parity with the Python executor).
 */
async function finalizeOutput<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  input: z.infer<In>,
  output: z.infer<Out>,
  ctx: ActivityContext,
  observation: ActivityObservation,
  moderationPolicyBlock?: ModerationPolicyBlock,
): Promise<z.infer<Out>> {
  let hooked: z.infer<Out>;
  if (descriptor.hook === undefined) {
    hooked = output;
  } else {
    // Record the hook span (input + transformed output / error) for hooked activities.
    const hookObservation = observation.observeHook({ input, output });
    try {
      hooked = await runHook(descriptor, input, output, ctx);
      hookObservation.updateOutput(hooked);
    } catch (error) {
      hookObservation.updateError(error);
      throw error;
    }
  }
  return applyModeration(descriptor.name, descriptor.moderation, hooked, moderationPolicyBlock, (verdict) =>
    // Redaction-exempt audit evidence (Python `_record_moderation_verdict`): a
    // dedicated TOP-LEVEL key so a backend's replace-not-merge update_metadata can't
    // clobber the activity's base metadata (preserved by DEFAULT_EXCLUDED_PATHS).
    observation.updateMetadata({ typeflux_moderation: verdict }),
  );
}

/**
 * Resolve the messages + effective model for a call: use caller-supplied
 * `messages`, else resolve the activity's `prompt` ref via `registry` and render
 * it against the input. The resolved prompt's model is the default when the call
 * sets none. Throws if neither `messages` nor `registry` is provided.
 */
async function resolveCall<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: AiActivityDescriptor<In, Out>,
  input: z.infer<In>,
  options: ExecuteActivityOptions,
): Promise<{
  messages: ChatMessage[];
  model: string | undefined;
  providerParams: Record<string, unknown>;
  /** The manifest's prompt provenance: the UNRENDERED template messages, the
   * registry's resolved version, and whether the prompt supplied the model. */
  promptMessages: ChatMessage[];
  resolvedPromptVersion: string | null;
  promptModel: string | null;
}> {
  if (options.messages !== undefined) {
    const providerParams = mergeProviderParams(options.providerParams, descriptor.providerParams);
    // The same params-model fallback as the registry path (Bugbot #526): a model
    // inside the call/activity params must select the call's model, not only fold
    // into the cache key while a different model actually runs.
    const paramsModel = providerParams["model"];
    return {
      messages: options.messages,
      model:
        options.model ?? (typeof paramsModel === "string" && paramsModel !== "" ? paramsModel : undefined),
      providerParams,
      promptMessages: options.messages,
      resolvedPromptVersion: null,
      promptModel: null,
    };
  }
  if (options.registry !== undefined) {
    const resolved = await options.registry.resolve(descriptor.prompt);
    const messages = renderMessages(resolved.messages, input as Record<string, unknown>);
    // Python precedence (#495): call defaults < prompt < activity params. WITHIN the
    // prompt layer the dedicated temperature/model fields beat the prompt's own params
    // record (Python `with_legacy` merges the legacy fields OVER the record); the
    // prompt's model rides as a layer value so an activity params model outranks it;
    // `options.model` stays the explicit TS call-level override on top.
    const providerParams = mergeProviderParams(
      options.providerParams,
      resolved.providerParams,
      { temperature: resolved.temperature, model: resolved.model },
      descriptor.providerParams,
    );
    // Python truthiness: an empty-string params model never selects a model.
    const paramsModel = providerParams["model"];
    const paramsModelOrUndefined =
      typeof paramsModel === "string" && paramsModel !== "" ? paramsModel : undefined;
    const promptParamsModel = resolved.providerParams?.["model"];
    return {
      messages,
      model: options.model ?? paramsModelOrUndefined,
      providerParams,
      promptMessages: resolved.messages,
      resolvedPromptVersion: resolved.resolvedVersion ?? null,
      // Python provider_model_source checks the prompt's provider_params
      // model too (presence, not truthiness) — a prompt selecting its model
      // only via params is still prompt_config (Bugbot).
      promptModel:
        resolved.model ?? (typeof promptParamsModel === "string" ? promptParamsModel : null),
    };
  }
  throw new Error(
    `executeActivity for ${JSON.stringify(descriptor.name)} requires either ` +
      "options.messages (pre-rendered) or options.registry (to resolve the prompt ref)",
  );
}

/**
 * A stable provider name (Python `provider_identifier`): the provider's declared
 * `providerName`, else its kebab-cased class name minus the `Provider` suffix
 * (`AnthropicProvider` -> `anthropic`), else the literal `"provider"`.
 *
 * Deliberate fork: Python's `no_session_cache_handle` labels an UNNAMED provider's
 * fail-soft handle `"unknown"`, while TS uses this identifier everywhere — a
 * kebab-cased class name in history/manifests beats an anonymous "unknown".
 * Identity hashes agree in both SDKs (both fold the full identifier).
 */
export function providerIdentifier(provider: unknown): string {
  const declared = (provider as { providerName?: unknown } | null)?.providerName;
  if (typeof declared === "string" && declared !== "") {
    return declared;
  }
  const className =
    typeof provider === "object" && provider !== null ? (provider.constructor?.name ?? "") : "";
  const trimmed = className.endsWith("Provider") ? className.slice(0, -"Provider".length) : className;
  const kebab = trimmed
    .split("")
    .map((char, index) => (index > 0 && char >= "A" && char <= "Z" ? `-${char.toLowerCase()}` : char.toLowerCase()))
    .join("");
  return kebab !== "" ? kebab : "provider";
}

export interface PrepareSessionCacheOptions {
  provider: ModelProvider;
  /**
   * The stable prefix messages, pre-resolved. Wins over `registry` (like `executeActivity`).
   * MUST be the static/unrendered prefix: `assertStableSystemPrefix` detects `{{var}}`
   * placeholders, not already-rendered per-item values — passing one item's rendered messages
   * here would poison the cached prefix for every other item. Prefer `registry`, which
   * resolves the prompt unrendered (the Python-parity path).
   */
  messages?: ChatMessage[];
  /** Resolves the descriptor's prompt ref; the prompt is used UNRENDERED (the stable prefix). */
  registry?: PromptRegistry;
  model?: string;
  providerParams?: Record<string, unknown>;
  /**
   * ISO-8601 wall-clock supplied by the CALLER (the cache-prep activity, PR6) and stamped on the
   * returned handle — never taken inside workflow code, so replay stays deterministic.
   */
  createdAt: string;
  /**
   * A representative map item: reference-style providers resolve the activity's
   * `cache_role: "reference"` artifacts from it (they must be identical across items, #363).
   */
  inputValue?: unknown;
  /** The worker-injected artifact resolver (as in `executeActivity`), for reference artifacts. */
  artifactResolver?: (
    inputValue: unknown,
    artifactInputs: readonly ArtifactInput[],
  ) => Promise<readonly ResolvedArtifactGroup[]> | readonly ResolvedArtifactGroup[];
  /**
   * Receives fail-soft degradation notices (Python logs these at WARNING). Defaults to
   * `console.warn` so a silently-uncached fleet is visible; inject the worker's logger in PR6.
   */
  onWarning?: (message: string) => void;
  /**
   * Per-call model policy guard (#454): the prep step issues a REAL provider call
   * (`prepareCachedSession`) under the resolved prefix model, so it is guarded like
   * the per-item calls — a disallowed model refuses the prep, not just the fan-out.
   */
  providerModelGuard?: ProviderModelGuard;
}

/**
 * Prepare a provider-side cached session for a map step's stable prefix (#478;
 * Python `executor.py::prepare_session_cache`). Run once before a map fan-out
 * (as its own activity, PR6), so the resulting handle is recorded in history
 * and replay-safe.
 *
 * FAIL-SOFT: if caching is not configured/enabled, the provider lacks the
 * capability, there is nothing stable to cache, reference-artifact resolution
 * fails, or the provider's own preparation fails (quota/tier caps, transient
 * API errors), returns the content-free fallback handle (`supported: false`) so
 * per-item calls behave exactly as today — caching is an optimization and must
 * never fail the workflow. A statically unstable prefix (`{{var}}` in a cached
 * system message) is the one LOUD failure: a developer misconfiguration, not a
 * runtime condition.
 *
 * Parity notes: TS has no model-policy guard yet (#495); the identity's output
 * schema is the provider-safe JSON schema rather than Pydantic's
 * `model_json_schema` (a fail-safe identity divergence — a miss, never a stale
 * hit). `model`/`providerParams` resolve with the SAME merge precedence as
 * `executeActivity` (call defaults < prompt < activity), so the prep and the
 * fan-out always agree on the effective behavior.
 */
export async function prepareSessionCache<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  options: PrepareSessionCacheOptions,
): Promise<CachedSessionHandle> {
  if (descriptor.kind === "code") {
    // A code activity rejects `sessionCache` at definition, so the worker never registers a
    // cache-prep companion for one — reaching here is a caller bug. This runtime guard stays
    // because the public signature accepts the union (the worker registers descriptors from a
    // heterogeneous map), so the type system cannot discharge it at this boundary; it also
    // narrows `descriptor` to the AI member for the rest of the function (prompt is required).
    throw new Error(
      `prepareSessionCache for ${JSON.stringify(descriptor.name)}: a pure-code activity has no ` +
        "provider session to cache (#746)",
    );
  }
  const provider = options.provider;
  const providerName = providerIdentifier(provider);
  const warn = options.onWarning ?? ((message: string) => console.warn(message));
  let prefixSource: ChatMessage[];
  let effectiveModel = options.model;
  let effectiveParams: Record<string, unknown>;
  if (options.messages !== undefined) {
    prefixSource = options.messages;
    effectiveParams = mergeProviderParams(options.providerParams, descriptor.providerParams);
    // Same params-model fallback as the registry branch (Bugbot #526).
    const messagesParamsModel = effectiveParams["model"];
    effectiveModel =
      options.model ??
      (typeof messagesParamsModel === "string" && messagesParamsModel !== "" ? messagesParamsModel : undefined);
  } else if (options.registry !== undefined) {
    // UNRENDERED (Python parity): the stable prefix must not interpolate any item.
    const resolved = await options.registry.resolve(descriptor.prompt);
    prefixSource = [...resolved.messages];
    // The same model + params resolution as executeActivity's resolveCall: a
    // prompt-pinned model must reach the prep + identity, or a model-scoped
    // (reference-style) cache is prepared under a different model than the fan-out.
    effectiveParams = mergeProviderParams(
      options.providerParams,
      resolved.providerParams,
      { temperature: resolved.temperature, model: resolved.model },
      descriptor.providerParams,
    );
    const paramsModel = effectiveParams["model"];
    effectiveModel =
      options.model ?? (typeof paramsModel === "string" && paramsModel !== "" ? paramsModel : undefined);
  } else {
    throw new Error(
      `prepareSessionCache for ${JSON.stringify(descriptor.name)} requires either ` +
        "options.messages (the stable prefix) or options.registry (to resolve the prompt ref)",
    );
  }
  // Guard the prep model BEFORE the fail-soft handle or the real prepareCachedSession
  // call — a policy-disallowed model must not establish a cached prefix (#454).
  invokeProviderModelGuard(options.providerModelGuard, {
    providerName,
    model: effectiveModel,
    activityName: descriptor.name,
    promptName: descriptor.prompt.name,
  });
  const systemMessages = prefixSource.filter((message) => message.role === "system");
  const identityOf = (
    messages: readonly ChatMessage[],
    referenceArtifacts: readonly ResolvedArtifactGroup[] = [],
  ): string =>
    sessionCacheIdentity({
      providerName,
      ...(effectiveModel !== undefined ? { model: effectiveModel } : {}),
      // Operational fields (timeout) shape the transport call, not the cached
      // prefix — a timeout change must not re-identify the session (#495).
      ...(Object.keys(behaviorProviderParams(effectiveParams)).length > 0
        ? { providerParams: behaviorProviderParams(effectiveParams) }
        : {}),
      systemMessages: messages,
      referenceArtifacts,
      outputSchema: descriptor.outputProviderSchema,
    });

  const config = descriptor.sessionCache;
  if (config === undefined || config.enabled === false || !supportsSessionCache(provider)) {
    // Cheap fail-soft: don't resolve artifacts when caching is off/unavailable.
    return noSessionCacheHandle({
      provider: providerName,
      identityHash: identityOf(systemMessages),
      model: effectiveModel ?? null,
    });
  }

  // Reference artifacts join the cached prefix only for reference-style providers
  // (they upload it once); prefix-style re-sends the prefix per item and caches it
  // server-side, so resolving the document here would just be a wasted read.
  const referenceInputs = (descriptor.artifacts ?? []).filter(
    (artifactInput) => artifactInput.cache_role === "reference",
  );
  const isReferenceStyle = sessionCacheStyleOf(provider) === "reference";
  let referenceGroups: readonly ResolvedArtifactGroup[] = [];
  let prefixMessages: ChatMessage[] = systemMessages;
  if (isReferenceStyle && referenceInputs.length > 0 && options.inputValue !== undefined) {
    try {
      if (options.artifactResolver === undefined) {
        throw new Error("no artifactResolver injected (supply the worker package's resolver)");
      }
      referenceGroups = await options.artifactResolver(options.inputValue, referenceInputs);
      prefixMessages = attachArtifactMessages(systemMessages, referenceInputs, referenceGroups);
    } catch (error) {
      // Resolving the reference artifact from the representative item failed (missing
      // path, unreadable file, policy violation). Degrade to uncached: the per-item
      // path sends full context and surfaces a real, per-item error if one exists.
      warn(
        `reference-artifact resolution for cache prep failed for ${JSON.stringify(descriptor.name)} ` +
          `(${error instanceof Error ? error.name : typeof error}); proceeding uncached`,
      );
      return noSessionCacheHandle({
        provider: providerName,
        identityHash: identityOf(systemMessages),
        model: effectiveModel ?? null,
      });
    }
  }

  // Identity covers the ASSEMBLED prefix (system + how reference artifacts attach —
  // role/text) plus the artifact bytes, so changing an attachment prompt or moving
  // it produces a distinct identity (Python #363 review).
  const identityHash = identityOf(prefixMessages, referenceGroups);

  // Nothing stable to cache: a "supported" handle would be a silent no-op (the
  // prefix breakpoint never lands), so report no caching honestly. Prefix-style
  // deliberately leaves referenceGroups empty (the doc is re-sent per item, not
  // uploaded at prep), so its declared reference INPUTS count as stable content
  // here (#362 review): a no-system activity whose only stable content is the
  // reference document must still engage the cache.
  const prefixReferenceInputs = isReferenceStyle ? [] : referenceInputs;
  if (systemMessages.length === 0 && referenceGroups.length === 0 && prefixReferenceInputs.length === 0) {
    return noSessionCacheHandle({ provider: providerName, identityHash, model: effectiveModel ?? null });
  }

  // A per-item template variable in the system prefix means it is not stable across
  // items — refuse LOUDLY rather than cache one item's rendered values.
  assertStableSystemPrefix(systemMessages);

  let handle: CachedSessionHandle;
  try {
    if (provider.prepareCachedSession === undefined) {
      throw new Error("provider declares supportsSessionCache but implements no prepareCachedSession");
    }
    handle = await provider.prepareCachedSession({
      messages: prefixMessages,
      artifacts: referenceGroups,
      ...(effectiveModel !== undefined ? { model: effectiveModel } : {}),
      ...(Object.keys(effectiveParams).length > 0 ? { providerParams: effectiveParams } : {}),
      identityHash,
      ...(config.ttlSeconds !== undefined ? { ttlSeconds: config.ttlSeconds } : {}),
    });
  } catch (error) {
    // A provider that CLAIMS the capability can still fail to prepare at runtime
    // (quota/tier caps, transient API errors). Degrade to uncached rather than fail
    // the whole map: Temporal retries would not help a permanent cap and would only
    // storm the provider.
    warn(
      `session cache preparation failed for activity ${JSON.stringify(descriptor.name)} ` +
        `(${error instanceof Error ? error.name : typeof error}); proceeding uncached`,
    );
    return noSessionCacheHandle({ provider: providerName, identityHash, model: effectiveModel ?? null });
  }
  // Stamp creation time here (the prep activity), never in workflow code, so it is
  // recorded once in history and stays constant on replay. reference_cached reflects
  // what was TRULY cached, not intent: an optional reference artifact missing on the
  // representative item yields an empty group, which must NOT flip the flag (items
  // that do carry it would then silently lose it). It also requires the returned
  // handle to be ENGAGED: a provider may decline politely (supported: false, no
  // throw), and the per-item skip gates on this flag alone — flipping it for an
  // unengaged handle would drop documents that were never cached (Bugbot #515).
  const referenceCached = handle.supported && referenceGroups.some((group) => group.artifacts.length > 0);
  // Prefix-style caches don't upload reference artifacts at prep (they re-send the prefix
  // per item); instead the handle carries the static count of stable reference turns so the
  // per-item Anthropic breakpoint can extend the cache over them (#362). Reference-style and
  // fail-soft handles leave it null. 0 ⇒ null so the provider keeps its legacy
  // conversation[-2] contract.
  let prefixStableMessages: number | null = null;
  // Prefix-style also flags whether per-item (non-reference) artifact turns will trail the
  // varying input (#698): with no leading reference span to mark, that shape has no stable
  // conversation span, so the per-item Anthropic breakpoint must skip the conversation rather
  // than mis-mark the varying query at [-2]. Reference-style and fail-soft handles leave it false.
  let perItemArtifactMessages = false;
  if (handle.supported && handle.style === "prefix") {
    const count = prefixStableMessageCount(descriptor);
    prefixStableMessages = count > 0 ? count : null;
    perItemArtifactMessages = prefixHasPerItemArtifacts(descriptor);
  }
  return {
    ...handle,
    created_at: options.createdAt,
    reference_cached: referenceCached,
    prefix_stable_messages: prefixStableMessages,
    per_item_artifact_messages: perItemArtifactMessages,
  };
}

/**
 * True when the per-item message assembly must promote reference artifacts into the cached
 * prefix (#362): the session cache is engaged, prefix-style, and at least one
 * `cache_role: "reference"` input is still present (a reference-style hit has already dropped
 * them, so this is only ever the prefix lane).
 */
function prefixReferenceCompositionApplies(
  cachedSession: CachedSessionHandle | undefined,
  artifactInputs: readonly ArtifactInput[],
): boolean {
  return (
    cachedSession != null &&
    cachedSession.supported &&
    cachedSession.style === "prefix" &&
    artifactInputs.some((artifactInput) => artifactInput.cache_role === "reference")
  );
}

/**
 * Assemble the prefix-style per-item message list (#362, D362-2):
 * `[rendered system…, reference attach…, rendered non-system…, per-item attach…]`. Reference
 * attach messages insert immediately after the rendered system block so they sit at the front
 * of the conversation (the cacheable prefix); the varying per-item turn and any per-item
 * artifact attachments stay behind them, exactly where append-last would have put them today.
 */
function composePrefixStableMessages(
  renderedMessages: readonly ChatMessage[],
  artifactInputs: readonly ArtifactInput[],
  artifactGroups: readonly ResolvedArtifactGroup[],
): ChatMessage[] {
  const referenceInputs = artifactInputs.filter((artifactInput) => artifactInput.cache_role === "reference");
  const perItemInputs = artifactInputs.filter((artifactInput) => artifactInput.cache_role !== "reference");
  const systemMessages = renderedMessages.filter((message) => message.role === "system");
  const nonSystemMessages = renderedMessages.filter((message) => message.role !== "system");
  // attachArtifactMessages appends onto its base; an empty base yields just the attach messages
  // for the requested inputs (group lookup spans all resolved artifacts, so passing the shared
  // `artifactGroups` for both slices is correct).
  const referenceAttach = attachArtifactMessages([], referenceInputs, artifactGroups);
  const perItemAttach = attachArtifactMessages([], perItemInputs, artifactGroups);
  return [...systemMessages, ...referenceAttach, ...nonSystemMessages, ...perItemAttach];
}

/**
 * Static count of the reference-artifact turns that will lead the prefix-style per-item
 * conversation (#362, D362-3). Only reference inputs that attach as user/assistant turns count
 * — a system-role reference rides in the (already cached) system block, not the conversation,
 * so it must not shift the Anthropic breakpoint index. Needs no artifact resolution: attach
 * roles are static.
 */
function prefixStableMessageCount<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
): number {
  return (descriptor.artifacts ?? []).filter(
    (artifactInput) =>
      artifactInput.cache_role === "reference" &&
      artifactInput.attach !== undefined &&
      artifactInput.attach.role !== "system",
  ).length;
}

/**
 * Whether the prefix-style per-item conversation will carry trailing per-item (non-reference)
 * artifact attach turns after the varying per-item input (#698). Those make the legacy
 * `conversation[-2]` breakpoint unsound: the last turn is a per-item artifact, so `-2` lands on
 * the varying query rather than a stable instructions turn. Only non-system attaches count — a
 * system-role artifact folds into the (cached) system block, not the conversation. Static (attach
 * roles are declared, no resolution needed); conservatively true even for an optional per-item
 * artifact, which at worst forgoes conversation caching for an item where it resolves empty (the
 * system block still caches).
 */
function prefixHasPerItemArtifacts<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
): boolean {
  return (descriptor.artifacts ?? []).some(
    (artifactInput) =>
      artifactInput.cache_role !== "reference" &&
      artifactInput.attach !== undefined &&
      artifactInput.attach.role !== "system",
  );
}

function cacheKeyFor<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  messages: ChatMessage[],
  model: string | undefined,
  providerParamsOption: Record<string, unknown> | undefined,
  tenant: Record<string, string>,
  artifacts: readonly ResolvedArtifactGroup[],
  cachedSession: CachedSessionHandle | undefined,
): CacheKey {
  // The model used by structuredCall must enter the key even when providerParams
  // is also given, or two models share one entry. Operational fields (timeout) are
  // excluded — they shape the transport call, not the output (#495).
  const providerParams: Record<string, unknown> = behaviorProviderParams(providerParamsOption ?? {});
  if (model !== undefined) {
    providerParams["model"] = model;
  }
  if (cachedSession != null) {
    // ANY handle folds (fail-soft included, Python parity): the session identity
    // covers reference-cached artifacts that per-item messages no longer carry,
    // and a cached vs uncached run must not share a cross-run entry. `!= null`
    // because an untyped JS caller may pass null where Python passes None.
    providerParams["__session_identity"] = cachedSession.identity_hash;
  }
  const inputHash = cacheInputHash({
    activity: descriptor.name,
    inputSchemaHash: descriptor.inputSchemaHash,
    renderedMessagesHash: messagesHash(messages),
    providerParams,
    // The resolved artifacts' identity (#504): an artifact part renders as only its group
    // name + preamble text, so without this fold swapping the underlying bytes (or the
    // source location, for unhashed artifacts) under the same group name would serve a
    // stale cached output.
    artifacts: artifactGroupsCacheIdentity(artifacts),
  });
  return cacheKey(descriptor.name, inputHash, tenant);
}

/**
 * Execute a pure-code activity (#746): run the deterministic handler, parse its result against the
 * output schema, then run the post-output hook — with NO provider call, NO repair loop, and NO
 * cache. Emits an activity observation span WITHOUT a generation child (there is no provider call
 * to observe). Because a code activity rejects `cache`/`sessionCache`/`moderation` at definition,
 * there is no cache-write-before-hook ordering to get wrong (the #745 hazard): validate → hook is
 * the entire pipeline, so a hook transform can never be bypassed by a cached read/write.
 */
async function executeCodeActivity<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: CodeActivityDescriptor<In, Out>,
  input: z.infer<In>,
  ctx: ActivityContext,
  options: ExecuteActivityOptions,
): Promise<z.infer<Out>> {
  // Parse the input BEFORE the handler so schema defaults/coercions apply on the DIRECT path
  // too, not only behind the worker boundary's parse (which is the AI path's input-validation
  // site — `buildTemporalActivity` parses and a bad payload rejects with a ZodError before any
  // execution). Same taxonomy here: a raw ZodError, thrown before the observation opens,
  // exactly as a worker-boundary parse failure produces no span. Idempotent under the worker
  // (parse of an already-parsed value).
  const parsedInput = descriptor.input.parse(input) as z.infer<In>;
  const observer = options.observer ?? NO_OP_OBSERVER;
  const observation = observer.observeActivity({
    activityName: descriptor.name,
    input: parsedInput,
    // No prompt/provider call: the span carries no rendered messages and no model.
    messages: [],
    model: null,
    tenant: ctx.tenant,
    ...(ctx.workflowId !== undefined ? { workflowId: ctx.workflowId } : {}),
    ...(ctx.runId !== undefined ? { runId: ctx.runId } : {}),
  });
  if (observer !== NO_OP_OBSERVER) {
    // The Temporal correlation join keys (#686) + the activity name, as on the AI path — a
    // control-plane-started run joins its trace to the execution id from the activity span.
    // No full execution manifest: that projection is prompt/provider-shaped and has no code analogue.
    observation.updateMetadata({
      "typeflux.activity_name": descriptor.name,
      ...(ctx.workflowId !== undefined ? { "temporal.workflow_id": ctx.workflowId } : {}),
      ...(ctx.runId !== undefined ? { "temporal.run_id": ctx.runId } : {}),
    });
  }
  try {
    throwIfCancelled(options.cancellationSignal);
    const raw = await descriptor.handler(parsedInput);
    // A cancel that landed while the handler ran must win before the result is finalized.
    throwIfCancelled(options.cancellationSignal);
    const result = descriptor.output.safeParse(raw);
    if (!result.success) {
      // No repair loop for deterministic code: a single output mismatch is terminal (attempts = 1).
      // ActivityValidationError is a TERMINAL error, so a worker fails it non-retryably. The catch
      // below records it on the observation.
      throw new ActivityValidationError(descriptor.name, 1, result.error);
    }
    const output = result.data as z.infer<Out>;
    // #745: input-aware, pre-acceptance output check. No repair loop exists for
    // deterministic code (consistent with the #746 code-path taxonomy), so a violation
    // is TERMINAL — an ActivityValidationError naming the violations, recorded on the
    // observation by the catch below. Runs BEFORE the hook (pre- vs post-acceptance).
    const violations = runOutputCheck(descriptor, parsedInput, output);
    if (violations !== undefined) {
      throw new ActivityValidationError(descriptor.name, 1, violations);
    }
    // finalizeOutput runs the hook (post-output) + moderation; a code activity has no moderation
    // (rejected at definition), so this is hook-only, and the hook re-validates its result like AI.
    const finalized = await finalizeOutput(descriptor, parsedInput, output, ctx, observation);
    observation.updateOutput(finalized);
    return finalized;
  } catch (error) {
    observation.updateError(error);
    throw error;
  } finally {
    observation.end();
  }
}

/**
 * Execute a typed activity (#745 ordering): cache check (a hit re-runs the
 * outputCheck; a failing hit is treated as a miss and regenerates) →
 * structured-call + validation-repair (schema parse + outputCheck, sharing the
 * repair retries) → context hook → moderation → cache write (only after the
 * output is fully accepted — a rejected output is never cached). Returns the
 * validated (and hook-transformed) output.
 *
 * A pure-code descriptor (`kind: "code"`, #746) is dispatched to {@link executeCodeActivity}
 * before any prompt/provider resolution.
 */
export async function executeActivity<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  input: z.infer<In>,
  options: ExecuteActivityOptions,
): Promise<z.infer<Out>> {
  const tenant = options.tenant ?? {};
  const ctx: ActivityContext = {
    activityName: descriptor.name,
    tenant,
    deps: options.deps ?? null,
    // Overrides carry only optional Temporal fields, so they enrich without
    // clobbering activityName/tenant/deps.
    ...(options.context ?? {}),
  };

  // Pure-code activity (#746): no prompt to resolve, no provider call, no cache — dispatch to the
  // dedicated code path (which still runs the post-output hook and emits an observation span).
  if (descriptor.kind === "code") {
    return executeCodeActivity(descriptor, input, ctx, options);
  }

  const {
    messages: resolvedMessages,
    model: effectiveModel,
    providerParams: effectiveParams,
    promptMessages,
    resolvedPromptVersion,
    promptModel,
  } = await resolveCall(descriptor, input, options);

  // Per-call policy guard (#454), BEFORE any cache read or provider call: the model
  // is now fully resolved (prompt/activity overrides applied), so a dynamic model an
  // admission check could not see is enforced here.
  invokeProviderModelGuard(options.providerModelGuard, {
    providerName: providerIdentifier(options.provider),
    model: effectiveModel,
    activityName: descriptor.name,
    promptName: descriptor.prompt.name,
  });

  // Resolve + attach the descriptor's declared artifact groups BEFORE hashing/caching so the
  // attached messages AND the groups' bytes identity (#504) enter the cache key (Python
  // prep-order parity).
  // Pre-resolved groups (options.artifacts) win; else the injected resolver runs; declaring
  // artifacts with neither fails loud — the documents would silently vanish from the prompt.
  // Reference artifacts live in the provider cache when the prep step ACTUALLY cached them
  // (#478): gated on the handle's reference_cached flag — never the provider style — so a
  // supported reference handle that cached only the system prefix keeps sending the document
  // instead of silently dropping it. Their bytes fold into the key via __session_identity.
  const skippedReferenceGroups = new Set(
    options.cachedSession?.reference_cached === true
      ? (descriptor.artifacts ?? [])
          .filter((artifactInput) => artifactInput.cache_role === "reference")
          .map((artifactInput) => artifactInput.name)
      : [],
  );
  const artifactInputs = (descriptor.artifacts ?? []).filter(
    (artifactInput) => !skippedReferenceGroups.has(artifactInput.name),
  );
  // Pre-resolved groups for a skipped reference input are dropped too (Python resolves from
  // the FILTERED inputs, so its `prepared.artifacts` never contains them): the group must not
  // reach the provider payload or the #504 cache-key fold — its bytes are covered by
  // `__session_identity`, and a prompt `artifact_group` part naming it would otherwise attach
  // the document natively despite the cache.
  let artifactGroups =
    skippedReferenceGroups.size > 0
      ? options.artifacts?.filter((group) => !skippedReferenceGroups.has(group.name))
      : options.artifacts;
  if (artifactInputs.length > 0 && artifactGroups === undefined) {
    if (options.artifactResolver === undefined) {
      throw new Error(
        `activity ${JSON.stringify(descriptor.name)} declares artifacts but neither ` +
          "`artifacts` (pre-resolved groups) nor an `artifactResolver` was provided " +
          "(inject the worker package's artifactInputResolver(policy))",
      );
    }
    artifactGroups = await options.artifactResolver(input, artifactInputs);
  }
  if (artifactInputs.length > 0) {
    // Python's resolver ALWAYS runs in prep, so a required input can never silently attach
    // nothing; pre-resolved `options.artifacts` bypasses that pipeline, so restore the invariant
    // here — a required group must be present and non-empty whichever path produced the groups.
    for (const artifactInput of artifactInputs) {
      if (!artifactInput.required) {
        continue;
      }
      const group = (artifactGroups ?? []).find((g) => g.name === artifactInput.name);
      if (group === undefined || group.artifacts.length === 0) {
        throw new Error(
          `activity ${JSON.stringify(descriptor.name)} requires artifact input ` +
            `${JSON.stringify(artifactInput.name)} but the resolved groups contain no artifacts for it`,
        );
      }
    }
  }
  const baseMessages = prefixReferenceCompositionApplies(options.cachedSession, artifactInputs)
    ? // Prefix-style cache + reference artifacts still in play: lift the stable reference
      // documents to the FRONT of the conversation (after the system prefix) so they join
      // the cached prefix instead of trailing the varying per-item turn (#362).
      composePrefixStableMessages(resolvedMessages, artifactInputs, artifactGroups ?? [])
    : artifactInputs.length > 0
      ? // Every other path (uncached, reference-style, prefix-style with no reference inputs)
        // keeps today's append-last order byte-for-byte.
        attachArtifactMessages(resolvedMessages, artifactInputs, artifactGroups ?? [])
      : resolvedMessages;

  const observer = options.observer ?? NO_OP_OBSERVER;
  const observation = observer.observeActivity({
    activityName: descriptor.name,
    input,
    messages: baseMessages,
    model: effectiveModel ?? null,
    tenant,
    // The Temporal identity (worker context provider) lets transports group
    // every activity of one workflow run under one parent trace.
    ...(ctx.workflowId !== undefined ? { workflowId: ctx.workflowId } : {}),
    ...(ctx.runId !== undefined ? { runId: ctx.runId } : {}),
  });
  if (observer !== NO_OP_OBSERVER) {
    // The Temporal JOIN KEYS on the observation metadata (#686; Python's OTel
    // writer records the same flat `temporal.workflow_id`/`temporal.run_id`):
    // a control-plane-started run has no caller-side parent metadata, so the
    // activity spans are the only surface a correlation lookup can join the
    // trace to its execution id from. Written BEFORE (and independently of)
    // the guarded manifest below — identity must survive a manifest failure.
    const temporalIdentity = {
      ...(ctx.workflowId !== undefined ? { "temporal.workflow_id": ctx.workflowId } : {}),
      ...(ctx.runId !== undefined ? { "temporal.run_id": ctx.runId } : {}),
    };
    if (Object.keys(temporalIdentity).length > 0) {
      observation.updateMetadata(temporalIdentity);
    }
    // The FULL activity execution manifest on the activity observation
    // (Python semantic-metadata parity: `typeflux.activity_execution_manifest`
    // plus the flat join keys) — what the control plane's last-run
    // reconstruction reads from a trace backend. Attached at observation
    // open like Python (its initial manifest is built with attempt 0 before
    // the span opens); per-generation validation attempts live on the
    // generation observations. Skipped entirely for the no-op observer so
    // untraced runs pay no hashing cost. GUARDED: a manifest-construction
    // failure is an observability error and must never fail the activity or
    // leak an un-ended observation (finder).
    try {
      const executionManifest = buildActivityExecutionManifest({
        activityName: descriptor.name,
        inputSchema: { name: descriptor.inputSchemaName ?? "input", hash: descriptor.inputSchemaHash },
        outputSchema: {
          name: descriptor.outputSchemaName ?? "output",
          hash: descriptor.outputSchemaHash,
        },
        promptRef: descriptor.prompt,
        resolvedPromptVersion,
        providerModel: effectiveModel ?? null,
        // The model that actually RUNS wins in the recorded params: a
        // call-level `options.model` override outranks a lower-precedence
        // params model, so the manifest must describe the real call (codex).
        providerParams: {
          ...effectiveParams,
          ...(effectiveModel !== undefined ? { model: effectiveModel } : {}),
        } as ProviderParams,
        // TS-native hook identity: JS has no module/qualname, so the hook's
        // function name is the identity (Python records module.qualname).
        hookName: descriptor.hook !== undefined ? descriptor.hook.name || "hook" : null,
        ...(descriptor.definitionSource !== undefined
          ? { definitionSource: descriptor.definitionSource }
          : {}),
        promptMessages,
        renderedMessages: baseMessages,
        // Python `provider_model_source`: the activity's params model (ANY
        // string, presence not truthiness — Python checks `is not None`)
        // beats a prompt-supplied model.
        providerModelSource:
          typeof descriptor.providerParams?.["model"] === "string"
            ? "yaml_provider"
            : promptModel !== null
              ? "prompt_config"
              : "yaml_provider",
        validationAttempt: 0,
        // Python's safe_definition shape — attach keeps only the ROLE (the
        // free-form preamble text must not leak into redaction-exempt
        // typeflux metadata), empty media_types is omitted (codex).
        ...(descriptor.artifacts !== undefined && descriptor.artifacts.length > 0
          ? {
              artifactInputs: descriptor.artifacts.map(
                (artifact) =>
                  Object.fromEntries(
                    Object.entries({
                      name: artifact.name,
                      from_path: artifact.from_path,
                      required: artifact.required,
                      kind: artifact.kind ?? null,
                      media_types: artifact.media_types.length > 0 ? [...artifact.media_types] : null,
                      max_count: artifact.max_count ?? null,
                      max_bytes: artifact.max_bytes ?? null,
                      attach: artifact.attach !== undefined ? { role: artifact.attach.role } : null,
                      cache_role: artifact.cache_role ?? null,
                    }).filter(([, value]) => value !== null),
                  ) as Json,
              ),
            }
          : {}),
        // The RESOLVED artifacts (safe summaries: provenance hashes, never
        // bytes/paths): two executions with different files in the same
        // group must not share a manifest hash (codex).
        ...(artifactGroups !== undefined && artifactGroups.length > 0
          ? { artifacts: artifactGroupsSummary(artifactGroups) as Json[] }
          : {}),
      }) as Record<string, unknown>;
      observation.updateMetadata({
        "typeflux.activity_execution_manifest": executionManifest,
        "typeflux.activity_name": descriptor.name,
        "typeflux.manifest_hash": executionManifest["manifest_hash"] ?? null,
      });
    } catch (error) {
      console.error("activity execution manifest failed (trace metadata omitted):", error);
    }
  }

  try {
    // Entry checkpoint — also covers the cache path: a cancelled activity must not serve the
    // hit (parity with Python's cache-hit raise_if_cancelled).
    throwIfCancelled(options.cancellationSignal);
    const cacheActive = descriptor.cache?.enabled === true && options.cacheStore !== undefined;
    const key = cacheActive
      ? cacheKeyFor(
          descriptor,
          baseMessages,
          effectiveModel,
          // The MERGED params (#495): a prompt- or activity-level behavior change
          // must partition the cross-run cache like a call-level one always has.
          effectiveParams,
          tenant,
          artifactGroups ?? [],
          options.cachedSession,
        )
      : undefined;

    // The PARSED input (defaults materialized) for BOTH downstream consumers —
    // the outputCheck (#745) and the hook via finalizeOutput (Bugbot #749): the
    // Python executor passes `prepared.input_value` (parsed) to both, so a direct
    // TS call with a partial payload must not run the hook against a different
    // input shape than the check validated. Computed once, on every path (hit +
    // generation); a safeParse fallback never NEWLY rejects an input that
    // previously ran — an invalid input is already the caller's bug and stays raw.
    const parsedInputResult = descriptor.input.safeParse(input);
    const parsedInput: z.infer<In> = (
      parsedInputResult.success ? parsedInputResult.data : input
    ) as z.infer<In>;

    if (key !== undefined && options.cacheStore !== undefined) {
      const bypassEnv = descriptor.cache?.bypassReadsEnv;
      const bypassed = bypassEnv !== undefined && process.env[bypassEnv] !== undefined;
      if (!bypassed) {
        const cached = await options.cacheStore.get(key);
        // Ignore a stale entry whose output schema has evolved (the key tracks
        // input + prompt + params, not output shape): treat it as a miss so the
        // activity regenerates rather than parsing an incompatible cached output.
        if (cached !== undefined && cached.output_schema_hash === descriptor.outputSchemaHash) {
          // A cancel that landed during the awaited cache lookup must not serve the hit — the
          // side-effectful hook/moderation would run for a workflow that no longer wants the
          // result (parity with Python's post-lookup raise_if_cancelled before hooks).
          throwIfCancelled(options.cancellationSignal);
          const output = descriptor.output.parse(
            coerceOptionalNulls(descriptor.output, cached.output),
          ) as z.infer<Out>;
          // #745 review: re-run the outputCheck on the HIT path. The check is pure
          // over (parsed input, output) — both available here — and nothing
          // check-related folds into the cache key, so an entry cached before a
          // check was added/tightened would otherwise be served forever despite
          // now violating it. A failing hit is treated as a MISS: fall through to
          // the generation loop (full repair retries), and the accepted fresh
          // output re-caches over the stale entry.
          if (runOutputCheck(descriptor, parsedInput, output) === undefined) {
            observation.updateMetadata({ cacheHit: true });
            // The hook receives the PARSED input (Bugbot #749) — the same value the
            // outputCheck just validated against, and Python `prepared.input_value` parity.
            const finalized = await finalizeOutput(descriptor, parsedInput, output, ctx, observation, options.moderationPolicyBlock);
            observation.updateOutput(finalized);
            return finalized;
          }
        }
      }
    }

    // Reached the generation loop, so this execution did not serve from cache.
    observation.updateMetadata({ cacheHit: false });
    const messages = [...baseMessages];
    const runGenerationLoop = async (): Promise<z.infer<Out>> => {
    for (let attempt = 0; ; attempt += 1) {
      throwIfCancelled(options.cancellationSignal);
      const generation = observation.observeGeneration({
        messages: [...messages],
        model: effectiveModel ?? null,
        attempt,
      });
      // The provider reports token usage through this sink (#478): recorded on the
      // generation observation (a copy — the provider may reuse its object) and
      // forwarded to the caller's sink. Per-generation, so a validation-repair
      // retry's usage lands on its own observation.
      const usageSink = (usage: ProviderUsage): void => {
        generation.updateMetadata({ usage: { ...usage } });
        options.usageSink?.(usage);
      };
      let raw: unknown;
      try {
        raw = await callWithTransientRetry(
          () =>
            options.provider.structuredCall({
              messages,
              outputSchema: descriptor.outputProviderSchema,
              ...(effectiveModel !== undefined ? { model: effectiveModel } : {}),
              ...(Object.keys(effectiveParams).length > 0 ? { providerParams: effectiveParams } : {}),
              ...(options.cancellationSignal !== undefined ? { signal: options.cancellationSignal } : {}),
              ...(artifactGroups !== undefined ? { artifacts: artifactGroups } : {}),
              ...(options.cachedSession != null ? { cachedSession: options.cachedSession } : {}),
              usageSink,
            }),
          options,
        );
      } catch (error) {
        generation.updateError(error);
        throw error;
      }
      // A cancel that landed while a non-abort-aware provider ran to completion must still win
      // before the result is validated/cached/finalized — asyncio parity: Python's CancelledError
      // fires at the next await after the call. The spend is lost, but a cancelled workflow will
      // not consume the result either way.
      throwIfCancelled(options.cancellationSignal);
      const result = descriptor.output.safeParse(coerceOptionalNulls(descriptor.output, raw));
      if (result.success) {
        const output = result.data as z.infer<Out>;
        // #745: input-aware, pre-acceptance output check, evaluated alongside the
        // schema parse and BEFORE the output is accepted (cached / hooked). A throw
        // or non-empty violations list is a validation failure that feeds the SAME
        // repair-message path a schema-parse miss uses — so a hallucinated citation
        // gets its `validationRetries` chances to self-correct.
        const violations = runOutputCheck(descriptor, parsedInput, output);
        if (violations !== undefined) {
          const rendered = renderOutputCheckViolations(violations);
          // Record on the generation like a schema-parse failure would (updateError).
          generation.updateError(new Error(`outputCheck violations:\n${rendered}`));
          if (attempt >= descriptor.validationRetries) {
            throw new ActivityValidationError(descriptor.name, attempt + 1, violations);
          }
          messages.push({
            role: "user",
            content:
              "Previous response failed output validation. Return a corrected response " +
              `matching the requested schema. Validation error: ${rendered}`,
          });
          continue;
        }
        generation.updateOutput(raw);
        // #745 review: snapshot the accepted PRE-hook output BEFORE the hook runs — a
        // hook that mutates its `output` argument in place would otherwise contaminate
        // the cached value through the shared reference (the store would hold the
        // post-hook state, and every hit would double-transform). structuredClone is
        // the right copier here: a cached output is JSON-shaped by contract (it came
        // from the provider's structured JSON and must round-trip a CacheRecord
        // store), and a plain deep copy preserves the zod output values exactly —
        // re-parsing a JSON round-trip could re-run defaults/transforms and diverge.
        const cacheSnapshot: z.infer<Out> | undefined =
          key !== undefined && options.cacheStore !== undefined ? structuredClone(output) : undefined;
        // Finalize (hook + moderation) BEFORE the cross-run cache write (#745): the hook
        // is post-acceptance and may reject, so an outputCheck- or hook-rejected output
        // must never be cached and served next run. Cache only after full acceptance.
        // The hook receives the PARSED input (Bugbot #749) — the same value the
        // outputCheck validated against (Python `prepared.input_value` parity).
        const finalized = await finalizeOutput(descriptor, parsedInput, output, ctx, observation, options.moderationPolicyBlock);
        if (key !== undefined && options.cacheStore !== undefined && cacheSnapshot !== undefined) {
          // Cache the PRE-hook snapshot (the hook re-runs on every cache hit via
          // finalizeOutput), now that outputCheck passed and the hook did not throw.
          await options.cacheStore.set(
            key,
            cacheRecord({
              key,
              output: cacheSnapshot,
              createdAt: new Date().toISOString(),
              outputSchemaHash: descriptor.outputSchemaHash,
              // #715 slice 1: carry the execution's subjects onto the record so a
              // later per-subject invalidation can find it (never the cache key).
              ...(ctx.subjectIds !== undefined ? { subjects: ctx.subjectIds } : {}),
            }),
          );
        }
        observation.updateOutput(finalized);
        return finalized;
      }
      generation.updateError(result.error);
      if (attempt >= descriptor.validationRetries) {
        throw new ActivityValidationError(descriptor.name, attempt + 1, result.error);
      }
      messages.push({
        role: "user",
        content:
          "Previous response failed output validation. Return a corrected response " +
          `matching the requested schema. Validation error: ${result.error.message}`,
      });
    }
    };
    // Provider-limits admission (#529 PR B; Python worker parity): one slot held for
    // the WHOLE generation loop (validation attempts + transient retries), keyed per
    // provider/model. Selection + wait metadata land on the activity observation
    // (Python's provider_call_metadata). NOTE: a provider-constructor default model
    // is invisible here (as it is to the cache key) — model-level limits apply when
    // the model comes from the call/prompt/spec merge.
    const controller = options.providerLimitController;
    if (controller === undefined) {
      return await runGenerationLoop();
    }
    const selection = controller.select({
      providerName: providerIdentifier(options.provider),
      ...(effectiveModel !== undefined ? { providerModel: effectiveModel } : {}),
    });
    const limiter = controller.limiterFor(selection);
    if (limiter === undefined) {
      observation.updateMetadata({
        "typeflux.provider_controls": {
          ...providerPolicySelectionMetadata(selection),
          queued: false,
          throttled: false,
          queued_seconds: 0,
          throttled_seconds: 0,
        },
      });
      return await runGenerationLoop();
    }
    try {
      return await limiter.limit(
        async (wait) => {
          observation.updateMetadata({
            "typeflux.provider_controls": {
              ...providerPolicySelectionMetadata(selection),
              ...providerCallWaitMetadata(wait),
            },
          });
          return runGenerationLoop();
        },
        options.cancellationSignal !== undefined ? { signal: options.cancellationSignal } : undefined,
      );
    } catch (error) {
      // An abort during admission rejects with the signal's raw reason — normalize it
      // to the cooperative-cancellation shape (same pattern as the transient-retry catch).
      throwIfCancelled(options.cancellationSignal);
      throw error;
    }
  } catch (error) {
    observation.updateError(error);
    throw error;
  } finally {
    observation.end();
  }
}
