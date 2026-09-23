/**
 * The provider session-cache contract (#478 PR2; Python `core/contracts.py`
 * `CachedSessionHandle` + `execution/session_cache.py` + `providers/base.py`).
 * Pure model + identity: the content-free handle that crosses the Temporal
 * payload boundary, the deterministic identity hash over the stable cached
 * prefix, the stable-prefix guard, and the capability probes. The executor
 * orchestration (prepare/fail-soft threading) lives in `execute.ts`/`activity.ts`,
 * and the per-provider prefix / reference implementations ship in the provider
 * adapters (`anthropic-provider.ts`, `openai-provider.ts`, `gemini-provider.ts`) —
 * the #478 ladder is complete.
 *
 * The handle is CROSS-SDK WIRE DATA (snake_case, key-for-key with Python's
 * Pydantic serialization): it is recorded in Temporal history and manifests.
 * The identity hash follows Python's payload recipe and is byte-identical for
 * aligned shapes (pinned by cross-SDK test constants in both suites); where a
 * shape has no TS equivalent (Pydantic-specific output-schema serialization),
 * divergence is FAIL-SAFE — a mismatched identity only changes the
 * `__session_identity` cache-key fold, producing a miss, never a stale hit.
 */

import { createHash } from "node:crypto";

import { z } from "zod";

import type { ResolvedArtifactGroup } from "./artifacts.js";
import { canonicalJson } from "./canonical-json.js";
import { hasTemplateVariables } from "./render.js";
import type { ChatMessage } from "./manifest-hashing.js";
import type { JsonSchema } from "./provider-schema.js";

/**
 * Suffixes for the auto-generated cache-prep/release activities that bracket a
 * cached map step's fan-out (#478 PR6). The same strings as Python
 * (`execution/session_cache.py`), so cross-SDK observability reads one naming.
 */
export const CACHE_PREP_ACTIVITY_SUFFIX = ".__prepare_cache__";
export const CACHE_RELEASE_ACTIVITY_SUFFIX = ".__release_cache__";

export function cachePrepActivityName(activityName: string): string {
  return `${activityName}${CACHE_PREP_ACTIVITY_SUFFIX}`;
}

export function cacheReleaseActivityName(activityName: string): string {
  return `${activityName}${CACHE_RELEASE_ACTIVITY_SUFFIX}`;
}

/** The provider's session-cache mechanism (Python `session_cache_style`). */
export type SessionCacheStyle = "reference" | "prefix";

/**
 * Reference to a provider-side cached prefix prepared once and reused across
 * many small per-item calls that share a large stable context (#60).
 *
 * Content-free by design: it carries only an identity hash of the stable
 * cached content (system prompt + reference artifacts + model/params) plus the
 * provider-native cache id — never the cached content itself — so it is safe
 * to pass through Temporal history and to record in manifests and traces.
 *
 * `supported: false` is the FAIL-SOFT fallback handle, returned for providers
 * without a session-cache capability (or when caching is disabled): callers
 * still receive a handle and the workflow shape is identical, but each call
 * sends the full context — today's behavior. Only cost/latency differ.
 */
export const cachedSessionHandleSchema = z
  .object({
    /** Provider name of the provider that prepared (or declined) the session. */
    provider: z.string(),
    /**
     * Deterministic hash over the stable cached content; ties a per-item call to
     * the prefix it reused and lets manifests/traces show cache identity safely.
     */
    identity_hash: z.string(),
    /**
     * True when caching is ENGAGED for this session. This does NOT imply a
     * server-side cache object: prefix-style providers (Anthropic/OpenAI) cache
     * implicitly with no object (`cache_id: null`), while reference-style
     * (Gemini) sets `cache_id`. False ⇒ fail-soft fallback.
     */
    supported: z.boolean().default(false),
    /**
     * The caching mechanism, carried on the handle so it stays self-describing
     * in Temporal history/manifests; null for the fail-soft fallback.
     */
    style: z.enum(["reference", "prefix"]).nullable().default(null),
    /**
     * Provider-native cache reference (e.g. a Gemini `cachedContent` name);
     * null for fallbacks and for providers whose caching is implicit (OpenAI).
     */
    cache_id: z.string().nullable().default(null),
    model: z.string().nullable().default(null),
    /**
     * ISO-8601 creation time; stamped by the preparing ACTIVITY (not at
     * construction), so the value stays deterministic for replay.
     */
    created_at: z.string().nullable().default(null),
    ttl_seconds: z.number().int().min(1).nullable().default(null),
    /**
     * True when the prep step actually included the activity's `cache: reference`
     * artifacts in this cached session (#363). The per-item path uses THIS as the
     * authoritative signal to drop those artifacts from per-item calls — never
     * the provider style alone — so a supported reference handle that cached only
     * the system prefix does not silently drop the document from every item.
     */
    reference_cached: z.boolean().default(false),
    /**
     * Prefix-style only (#362): the number of stable `cache: reference` artifact
     * turns that lead the per-item conversation — inserted after the rendered
     * system prefix and before the varying per-item turn. A positive int tells the
     * prefix-style provider (Anthropic) to mark the cache breakpoint on
     * `conversation[prefix_stable_messages - 1]` (the last stable turn), so the
     * reference documents join the cached prefix instead of trailing the variable
     * input. null (or 0) keeps the legacy `conversation[-2]` "stable instructions
     * turn" contract byte-for-byte. Reference-style leaves this null — its
     * artifacts live in the provider cache object, not the per-item conversation.
     * A content-free count, so the handle stays manifest-/history-safe.
     */
    prefix_stable_messages: z.number().int().nullable().default(null),
    /**
     * Prefix-style only (#698): true when the activity declares per-item (i.e. NOT
     * `cache_role: "reference"`) artifact attach turns that trail the varying
     * per-item input — the shape `[system, per-item query, per-item artifact attach]`.
     * There the legacy `conversation[-2]` breakpoint would land on the varying query
     * (a mis-mark: the last turn is the artifact, so `-2` is not a stable
     * instructions turn), keying the conversation cache on content that differs per
     * item. When true AND `prefix_stable_messages` is null/0 (no leading reference
     * span to mark instead), the provider marks NOTHING in the conversation — the
     * shape has no stable conversation span, only the system block is cached. Ignored
     * when `prefix_stable_messages` > 0 (the reference span is the authoritative
     * breakpoint). Reference-style and fail-soft handles leave it false. A
     * content-free boolean, so the handle stays manifest-safe.
     */
    per_item_artifact_messages: z.boolean().default(false),
  })
  .strict();

export type CachedSessionHandle = z.infer<typeof cachedSessionHandleSchema>;

/**
 * The fail-soft fallback handle (Python `no_session_cache_handle`): caching not
 * engaged, full context per call. Returned when the provider lacks the
 * capability, caching is disabled, there is nothing stable to cache, or prep
 * failed transiently — the workflow proceeds identically either way.
 */
export function noSessionCacheHandle(params: {
  provider: string;
  identityHash: string;
  model?: string | null;
}): CachedSessionHandle {
  return {
    provider: params.provider,
    identity_hash: params.identityHash,
    supported: false,
    style: null,
    cache_id: null,
    model: params.model ?? null,
    created_at: null,
    ttl_seconds: null,
    reference_cached: false,
    prefix_stable_messages: null,
    per_item_artifact_messages: false,
  };
}

/**
 * Structural capability probe (Python `supports_session_cache`, a getattr with
 * a False default): a provider opts in by declaring `supportsSessionCache: true`.
 * Absent or anything else means unsupported — the executor fails soft.
 */
export function supportsSessionCache(provider: unknown): boolean {
  return (
    typeof provider === "object" &&
    provider !== null &&
    (provider as { supportsSessionCache?: unknown }).supportsSessionCache === true
  );
}

/** The declared style of a session-cache-capable provider; undefined when absent/invalid. */
export function sessionCacheStyleOf(provider: unknown): SessionCacheStyle | undefined {
  if (typeof provider !== "object" || provider === null) {
    return undefined;
  }
  const style = (provider as { sessionCacheStyle?: unknown }).sessionCacheStyle;
  return style === "reference" || style === "prefix" ? style : undefined;
}

/**
 * A session-cached activity has a per-item template variable in its system
 * prefix, so the prefix is not stable across map items and cannot be cached.
 */
export class UnstableCachePrefixError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UnstableCachePrefixError";
  }
}

function messageTexts(message: ChatMessage): string[] {
  const content = message.content;
  if (typeof content === "string") {
    return [content];
  }
  // Any renderable text field counts: TextPart.text, but also the optional
  // ArtifactPart.text / ArtifactGroupPart.text — rendering templates all of
  // them, so a {{var}} in any would vary per item (Python #60 review).
  return content.flatMap((part) => {
    const text = (part as { text?: unknown }).text;
    return typeof text === "string" ? [text] : [];
  });
}

/**
 * Reject `{{var}}` placeholders in system messages of a cached activity
 * (Python `assert_stable_system_prefix`). Static, content-cheap detection:
 * raised at prep time so the failure is loud and per-activity, not a silent
 * cache miss or — worse — a cache poisoned with one item's values (#60).
 */
export function assertStableSystemPrefix(systemMessages: readonly ChatMessage[]): void {
  for (const message of systemMessages) {
    for (const text of messageTexts(message)) {
      if (hasTemplateVariables(text)) {
        throw new UnstableCachePrefixError(
          "session cache requires a stable system prefix, but a system message " +
            `contains a per-item template variable: ${JSON.stringify(text)}`,
        );
      }
    }
  }
}

/**
 * A stable, JSON-able identity for one content part (Python `_part_identity`).
 * TS parts carry the same `type` literals and field sets as Python's dataclasses,
 * whose identity emits every declared field (missing optionals as null).
 */
function partIdentity(part: ChatMessage["content"][number]): Record<string, unknown> {
  if (typeof part === "string") {
    // Unreachable for well-formed ChatContent (a string content never reaches the
    // per-part path), but keep the total function total.
    return { type: "text", text: part };
  }
  switch (part.type) {
    case "text":
      return { type: part.type, text: part.text };
    case "artifact":
      return { type: part.type, artifact: part.artifact, text: part.text ?? null };
    case "artifact_group":
      return { type: part.type, group: part.group, text: part.text ?? null };
    case "provider_extension":
      return { type: part.type, provider: part.provider, payload: { ...part.payload } };
  }
}

function messageIdentity(message: ChatMessage): Record<string, unknown> {
  const content = message.content;
  return {
    role: message.role,
    name: message.name ?? null,
    content: typeof content === "string" ? content : content.map(partIdentity),
  };
}

function groupIdentity(group: ResolvedArtifactGroup): Record<string, unknown> {
  // Identity over the artifact BYTES (sha256) + kind/media-type, never the
  // bytes themselves — same content ⇒ same identity, across items and workers.
  return {
    name: group.name,
    artifacts: group.artifacts.map((artifact) => ({
      sha256: artifact.sha256 ?? null,
      kind: artifact.kind ?? null,
      media_type: artifact.media_type ?? null,
    })),
  };
}

/**
 * Deterministic hex identity over the stable cached prefix (Python
 * `session_cache_identity`). Covers everything the provider would cache and
 * nothing per-item: the provider profile (caches are scoped to backend/region/
 * project), model, behavior params, the system messages, the reference-artifact
 * identities, and the output schema (it participates in the cached prefix for
 * structured output). It explicitly EXCLUDES operational fields (timeout, trace
 * metadata, workflow/run ids) and the per-item input.
 */
export function sessionCacheIdentity(params: {
  providerName?: string | null;
  providerProfile?: Record<string, unknown> | null;
  model?: string | null;
  providerParams?: Record<string, unknown> | null;
  systemMessages: readonly ChatMessage[];
  referenceArtifacts?: readonly ResolvedArtifactGroup[];
  outputSchema?: JsonSchema | null;
}): string {
  const payload = {
    provider: params.providerName ?? null,
    profile: params.providerProfile ?? {},
    model: params.model ?? null,
    params: params.providerParams ?? {},
    system: params.systemMessages.map(messageIdentity),
    artifacts: (params.referenceArtifacts ?? []).map(groupIdentity),
    output_schema: params.outputSchema ?? null,
  };
  return createHash("sha256").update(canonicalJson(payload), "utf-8").digest("hex");
}
