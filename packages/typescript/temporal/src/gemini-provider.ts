/**
 * A Gemini `ModelProvider` (Epic 2, #449). Sends the activity's provider-safe output
 * schema via `generationConfig.responseSchema` (+ `responseMimeType: "application/json"`)
 * and returns the parsed JSON (the executor validates it with Zod).
 *
 * The Gemini client is injected as a thin structural transport, so no `@google/genai`
 * dependency is added here — wire a real client with a one-line adapter (`cachedContent`
 * must be forwarded, or session-cached calls (#478) silently lose their cached prefix;
 * `caches` enables reference-style session caching and may be omitted otherwise):
 *
 *     new GeminiProvider({
 *       generateContent: ({ model, contents, systemInstruction, cachedContent, generationConfig }, opts) =>
 *         client.models.generateContent({
 *           model,
 *           contents,
 *           config: {
 *             systemInstruction, cachedContent, ...generationConfig,
 *             abortSignal: opts?.signal, httpOptions: { timeout: opts?.timeoutMs },
 *           },
 *         }),
 *       caches: {
 *         create: (params) => client.caches.create(params),
 *         delete: (params) => client.caches.delete(params),
 *       },
 *     }, { model });
 *
 * Tests pass a fake transport; any Gemini-compatible endpoint works.
 */

import { readFile, stat } from "node:fs/promises";

import { artifactForName, artifactsForGroup, type ResolvedArtifact, type ResolvedArtifactGroup } from "./artifacts.js";
import type { ChatContent } from "./content-parts.js";
import {
  abortableDelay,
  numberProviderParam,
  ProviderCacheUnavailableError,
  ProviderConfigError,
  raiseIfTruncated,
  ProviderRateLimitError,
  ProviderTransientError,
  retryAfterSecondsFrom,
  stopProviderParam,
  type ModelProvider,
  type PrepareCachedSessionParams,
  type ProviderUsage,
  type StructuredCallParams,
  callOptions,
  type TransportCallOptions,
} from "./execute.js";
import type { CachedSessionHandle } from "./session-cache.js";
import type { JsonSchema } from "./provider-schema.js";

// HTTP statuses worth retrying — rate limit, request timeout, conflict, and any 5xx.
const RETRYABLE_STATUS = new Set([408, 409, 429]);

function isRetryableTransportError(error: unknown): boolean {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === "number" && (RETRYABLE_STATUS.has(status) || status >= 500);
}

// 429 or an SDK error named RateLimitError (Python `matches_provider_error(...,
// "RateLimitError") or code == 429`) — classified BEFORE the generic transient wrap
// so the distinct class (+ Retry-After hint) survives (#529).
function isRateLimitTransportError(error: unknown): boolean {
  const record = error as { status?: unknown; name?: unknown } | null;
  return record?.status === 429 || record?.name === "RateLimitError";
}

/** A text part — what Gemini returns, and the text half of a request. Responses are read via `.text`. */
export interface GeminiPart {
  text?: string;
}

/**
 * A request content part: a text part, OR a native part (`inlineData`/`fileData`, …) supplied
 * verbatim via a `gemini` provider_extension. Responses are always typed as the strict `GeminiPart`,
 * so `extractStructuredOutput`'s `part.text` read stays type-checked (the index signature lives only
 * on the request side, where arbitrary native fields are intended).
 */
export type GeminiRequestPart = GeminiPart | Record<string, unknown>;

export interface GeminiContent {
  /** Gemini conversation roles are `user` and `model` (assistant). */
  role: string;
  parts: GeminiRequestPart[];
}

/** The `generateContent` request this provider builds (a subset of Gemini's). */
export interface GeminiGenerateContentRequest {
  model: string;
  contents: GeminiContent[];
  /** Gemini's system prompt is a top-level instruction, not a conversation role. */
  systemInstruction?: { parts: GeminiRequestPart[] };
  /**
   * The server-side `cachedContent` name to reference (#478, reference-style session
   * cache). When set, `systemInstruction` is OMITTED — the prefix lives in the cache
   * and must not also go inline. Adapters map it to the genai SDK's
   * `config.cachedContent`.
   */
  cachedContent?: string;
  generationConfig: {
    responseMimeType: "application/json";
    responseSchema: JsonSchema;
    maxOutputTokens?: number;
    /** Behavior params (#495) — set only when the merged call params carry them. */
    temperature?: number;
    topP?: number;
    topK?: number;
    seed?: number;
    stopSequences?: string[];
    /** The params `thinking_budget` (#495): 0 disables thinking (Gemini 2.5 flash). */
    thinkingConfig?: { thinkingBudget: number };
  };
}

export interface GeminiCandidate {
  content?: { parts?: GeminiPart[] };
  finishReason?: string;
}

/** The `generateContent` response this provider reads (a subset of Gemini's). */
export interface GeminiGenerateContentResponse {
  candidates?: GeminiCandidate[];
  /**
   * Token accounting (#478). `totalTokenCount` includes thinking tokens for reasoning
   * models, so it is forwarded verbatim rather than derived; `cachedContentTokenCount`
   * counts tokens served from a referenced `cachedContent` (a hit).
   */
  usageMetadata?: {
    promptTokenCount?: number;
    candidatesTokenCount?: number;
    totalTokenCount?: number;
    cachedContentTokenCount?: number;
  };
}

/** A Files API file (the slice the provider reads): `state` is the string form of FileState. */
export interface GeminiFile {
  name?: string;
  uri?: string;
  mimeType?: string;
  state?: string;
}

/** The Files API slice used for oversize artifacts (#503) — optional on the transport. */
export interface GeminiFilesTransport {
  upload(params: { file: string; config?: { mimeType?: string } }): Promise<GeminiFile>;
  get(params: { name: string }): Promise<GeminiFile>;
}

/**
 * The cachedContent API slice for reference-style session caching (#478) — optional on
 * the transport, like `files`. The `config` follows the JS genai SDK's camelCase shape.
 * Adapter: `caches: { create: (p, o) => client.caches.create(p), delete: (p, o) => client.caches.delete(p) }`.
 */
export interface GeminiCachesTransport {
  create(
    params: { model: string; config: Record<string, unknown> },
    options?: TransportCallOptions,
  ): Promise<{ name?: string }>;
  delete(params: { name: string }, options?: TransportCallOptions): Promise<unknown>;
}

/**
 * The minimal Gemini-style client slice the provider needs (wire a real client via a thin
 * adapter). `files` is optional: it is only touched when a referenced local artifact exceeds the
 * 20MB inline cap — a transport without it fails loud in that case (adapter:
 * `files: { upload: (p) => client.files.upload({ file: p.file, config: p.config }), get: (p) => client.files.get(p) }`).
 */
export interface GeminiGenerateContentTransport {
  generateContent(request: GeminiGenerateContentRequest, options?: TransportCallOptions): Promise<GeminiGenerateContentResponse>;
  files?: GeminiFilesTransport;
  /** Optional (#478): only touched by `prepareCachedSession`/`releaseCachedSession`. */
  caches?: GeminiCachesTransport;
}

export interface GeminiProviderOptions {
  /** Default model when a call doesn't set one. For cross-run cache correctness, pass the
   * effective model via `executeActivity`'s options (it enters the cache key). */
  model?: string;
  /** `maxOutputTokens` cap (omitted from the request when unset — Gemini has its own default). */
  maxOutputTokens?: number;
}

const DEFAULT_MODEL = "gemini-2.5-flash";

export class GeminiProvider implements ModelProvider {
  readonly providerName = "gemini";
  /** Session-cache capability (#478): reference style — a server-side `cachedContent`. */
  readonly supportsSessionCache = true;
  readonly sessionCacheStyle = "reference" as const;
  private readonly transport: GeminiGenerateContentTransport;
  private readonly defaultModel: string;
  private readonly maxOutputTokens: number | undefined;

  constructor(transport: GeminiGenerateContentTransport, options: GeminiProviderOptions = {}) {
    this.transport = transport;
    this.defaultModel = options.model ?? DEFAULT_MODEL;
    this.maxOutputTokens = options.maxOutputTokens;
    if (this.maxOutputTokens !== undefined && this.maxOutputTokens < 1) {
      throw new ProviderConfigError("GeminiProvider: maxOutputTokens must be >= 1", { provider: "gemini" });
    }
  }

  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    const model = params.model ?? this.defaultModel;
    // Fail LOUD on a malformed engaged handle rather than silently dropping the cached
    // prefix (Python _resolve_gemini_cache_id); undefined = not caching.
    const cachedContent = resolveGeminiCacheId(params.cachedSession, model);

    // Gemini's system prompt is a top-level `systemInstruction`; user/assistant turns map to
    // the `user`/`model` conversation roles.
    const artifacts = params.artifacts ?? [];
    // Oversize local artifacts upload via the Files API BEFORE parts are built, so the part
    // builder can reference them by uri (Python _upload_oversize_artifacts order).
    const uploaded = await uploadOversizeArtifacts(this.transport, params.messages, artifacts, params.signal);
    const systemParts: GeminiRequestPart[] = [];
    const contents: GeminiContent[] = [];
    for (const message of params.messages) {
      if (message.role === "system") {
        systemParts.push(...(await contentToParts(message.content, artifacts, uploaded)));
        continue;
      }
      if (message.role !== "user" && message.role !== "assistant") {
        throw new ProviderConfigError(`GeminiProvider: unsupported message role ${JSON.stringify(message.role)}`, {
          provider: "gemini",
        });
      }
      const parts = await contentToParts(message.content, artifacts, uploaded);
      if (parts.length === 0) {
        // An empty content array maps to zero parts; Gemini rejects a content item with no
        // parts. Fail with a clear local error rather than an opaque 400 (matching the
        // system-only guard below).
        throw new ProviderConfigError(
          `GeminiProvider: a ${message.role} message has empty content; Gemini requires at least one content part`,
          { provider: "gemini" },
        );
      }
      contents.push({ role: message.role === "user" ? "user" : "model", parts });
    }

    if (contents.length === 0) {
      // Gemini requires at least one user/model content item; fail with a clear local
      // error rather than an opaque provider-side 400 (parity with Python).
      throw new ProviderConfigError(
        "GeminiProvider: requires at least one user or assistant message; a system-only prompt has no content to generate from",
        { provider: "gemini" },
      );
    }

    const request: GeminiGenerateContentRequest = {
      model,
      contents,
      generationConfig: {
        responseMimeType: "application/json",
        responseSchema: params.outputSchema,
        // Behavior params (#495; Python _gemini_call_kwargs): forwarded only when present
        // and well-typed; a params max_tokens overrides the constructor cap. `seed` is
        // deliberately NOT mapped — Python's Gemini provider excludes it from its
        // supported set (a configured seed fails loud there), so sending it here would
        // diverge; `thinking_budget` -> thinking_config mapping is deferred to #495 A2.
        ...((() => {
          const behavior = params.providerParams;
          const maxOutputTokens = numberProviderParam(behavior, "max_tokens") ?? this.maxOutputTokens;
          const temperature = numberProviderParam(behavior, "temperature");
          const topP = numberProviderParam(behavior, "top_p");
          const topK = numberProviderParam(behavior, "top_k");
          const stop = stopProviderParam(behavior);
          // thinking_budget 0 is VALID (disables thinking) — the >= 0 check keeps it.
          const thinkingBudget = numberProviderParam(behavior, "thinking_budget");
          return {
            ...(maxOutputTokens !== undefined ? { maxOutputTokens } : {}),
            ...(temperature !== undefined ? { temperature } : {}),
            ...(topP !== undefined ? { topP } : {}),
            ...(topK !== undefined ? { topK } : {}),
            ...(stop !== undefined ? { stopSequences: stop } : {}),
            ...(thinkingBudget !== undefined && thinkingBudget >= 0
              ? { thinkingConfig: { thinkingBudget } }
              : {}),
          };
        })()),
      },
    };
    if (cachedContent !== undefined) {
      // The stable prefix (incl. system instruction) lives in the referenced cache; the
      // per-item call sends only the item contents (#478, reference style — Python's
      // exact elif). Gemini REJECTS system_instruction alongside cached_content, so any
      // assembled system parts are dropped here: for the stable prefix that is correct
      // (it IS the cache), but per-item system-role content beyond the prefix (e.g. a
      // non-reference artifact attached with role "system") is silently lost — a shared
      // Python/TS design constraint; validation-repair messages already use role "user"
      // for this reason, and the YAML load-time check (#478 PR6) rejects the config.
      request.cachedContent = cachedContent;
    } else if (systemParts.length > 0) {
      request.systemInstruction = { parts: systemParts };
    }

    let response: GeminiGenerateContentResponse;
    try {
      response = await this.transport.generateContent(request, callOptions(params));
    } catch (error) {
      throw classifyGeminiTransportError(error, cachedContent);
    }

    const candidate = response.candidates?.[0];
    raiseIfTruncated(candidate?.finishReason === "MAX_TOKENS", {
      provider: "gemini",
      message: "GeminiProvider: the response was truncated (finishReason MAX_TOKENS); increase maxOutputTokens",
    });
    reportGeminiUsage(response, params, model);
    return extractStructuredOutput(candidate);
  }

  /**
   * Upload the stable prefix to a server-side `cachedContent` (#478; Python
   * `prepare_cached_session`). Reference style: the system instruction + prefix
   * contents are stored once via `caches.create`; per-item calls reference the
   * returned name and do not re-send the prefix. Throws typed on any failure —
   * the executor's fail-soft ladder (PR3) turns a throw into an uncached run.
   * `created_at` is left for the prep activity to stamp (replay-safe).
   */
  async prepareCachedSession(params: PrepareCachedSessionParams): Promise<CachedSessionHandle> {
    const caches = this.transport.caches;
    if (caches === undefined) {
      throw new ProviderConfigError(
        "GeminiProvider: the transport has no `caches` surface; wire " +
          "`caches: { create: (p) => client.caches.create(p), delete: (p) => client.caches.delete(p) }` " +
          "to enable reference-style session caching",
        { provider: "gemini" },
      );
    }
    const model = params.model ?? this.defaultModel;
    // Oversize reference artifacts (e.g. a >20MB video) ride the Files API and are
    // referenced by uri inside the cached content, not inlined — caches.create has the
    // same inline cap as a normal request (Python #358 parity).
    const artifacts = params.artifacts;
    // No abort signal: prep runs in its own activity (Python parity — prepare is not abort-aware).
    const uploaded = await uploadOversizeArtifacts(this.transport, params.messages, artifacts, undefined);
    const systemParts: GeminiRequestPart[] = [];
    const contents: GeminiContent[] = [];
    for (const message of params.messages) {
      if (message.role === "system") {
        systemParts.push(...(await contentToParts(message.content, artifacts, uploaded)));
        continue;
      }
      if (message.role !== "user" && message.role !== "assistant") {
        throw new ProviderConfigError(`GeminiProvider: unsupported message role ${JSON.stringify(message.role)}`, {
          provider: "gemini",
        });
      }
      const parts = await contentToParts(message.content, artifacts, uploaded);
      if (parts.length === 0) {
        // Same guard as structuredCall: Gemini rejects a content item with no parts, and a
        // prepare throw lands in the executor's fail-soft ladder (behavioral parity with
        // Python, whose server-side rejection produces the same uncached outcome).
        throw new ProviderConfigError(
          `GeminiProvider: a ${message.role} prefix message has empty content; Gemini requires at least one content part`,
          { provider: "gemini" },
        );
      }
      contents.push({ role: message.role === "user" ? "user" : "model", parts });
    }
    if (systemParts.length === 0 && contents.length === 0) {
      throw new ProviderConfigError(
        "GeminiProvider: a cached session needs a stable prefix (system instructions or contents)",
        { provider: "gemini" },
      );
    }
    // camelCase config keys (systemInstruction), matching the JS genai SDK and this
    // transport's own request convention — Python's snake_case is a Python-SDK shape.
    const config: Record<string, unknown> = {};
    if (systemParts.length > 0) {
      config["systemInstruction"] = { parts: systemParts };
    }
    if (contents.length > 0) {
      config["contents"] = contents;
    }
    if (params.ttlSeconds !== undefined) {
      config["ttl"] = `${params.ttlSeconds}s`;
    }
    let cache: { name?: string };
    try {
      cache = await caches.create({ model, config });
    } catch (error) {
      throw classifyGeminiTransportError(error, undefined);
    }
    if (typeof cache.name !== "string" || cache.name === "") {
      throw new ProviderConfigError("GeminiProvider: caches.create returned no cache name", {
        provider: "gemini",
      });
    }
    return {
      provider: this.providerName,
      identity_hash: params.identityHash,
      supported: true,
      style: "reference",
      cache_id: cache.name,
      model,
      created_at: null,
      ttl_seconds: params.ttlSeconds ?? null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    };
  }

  /**
   * Delete the server-side cache, best-effort (Python parity): a fallback handle or a
   * transport without the surface has nothing to release, and a delete failure
   * (already expired / 404 / transient) is swallowed — the cache TTL-expires
   * regardless, so a failed cleanup must not fail the workflow.
   */
  async releaseCachedSession(handle: CachedSessionHandle): Promise<void> {
    const caches = this.transport.caches;
    if (handle.cache_id === null || handle.cache_id === "" || caches === undefined) {
      return;
    }
    try {
      await caches.delete({ name: handle.cache_id });
    } catch (error) {
      console.warn(
        `GeminiProvider: best-effort release of cache ${handle.cache_id} failed ` +
          `(${error instanceof Error ? error.name : typeof error}); TTL will reap it`,
      );
    }
  }
}

/**
 * The cachedContent name to reference, or undefined when not caching (Python
 * `_resolve_gemini_cache_id`). Fails LOUD rather than silently dropping the cached
 * prefix: an engaged reference-style handle MUST carry a cache_id, match this
 * provider, and match the call model (Gemini caches are model-bound).
 */
function resolveGeminiCacheId(
  cachedSession: CachedSessionHandle | undefined,
  model: string | undefined,
): string | undefined {
  if (cachedSession == null || !cachedSession.supported) {
    return undefined;
  }
  if (cachedSession.provider !== "gemini") {
    throw new ProviderConfigError(
      `GeminiProvider: cached session was prepared by ${JSON.stringify(cachedSession.provider)}, not gemini`,
      { provider: "gemini" },
    );
  }
  if (cachedSession.cache_id === null || cachedSession.cache_id === "") {
    throw new ProviderConfigError(
      "GeminiProvider: reference-style cached session has no cache_id; the cached prefix would be silently lost",
      { provider: "gemini" },
    );
  }
  // Python truthiness: both sides must be non-empty for the mismatch check to apply.
  if (
    cachedSession.model !== null &&
    cachedSession.model !== "" &&
    model !== undefined &&
    model !== "" &&
    cachedSession.model !== model
  ) {
    throw new ProviderConfigError(
      `GeminiProvider: cached session model ${JSON.stringify(cachedSession.model)} does not match ` +
        `call model ${JSON.stringify(model)} (Gemini caches are model-bound)`,
      { provider: "gemini" },
    );
  }
  return cachedSession.cache_id;
}

/**
 * Whether a per-item failure is the referenced cache having vanished (Python
 * `_is_stale_cache_error`): Gemini reports an expired/deleted cachedContent as
 * "403 PERMISSION_DENIED ... CachedContent not found (or permission denied)"
 * (live-verified in Python, #368). Match the status code (robust to message-wording
 * drift) OR the explicit CachedContent token (covers an odd-status variant). A
 * genuine auth failure recovers safely: the uncached retry hits the same failure
 * and ends terminally — one extra call, never a silent loss of recovery.
 */
function isStaleCacheError(error: unknown): boolean {
  const status = (error as { status?: unknown } | null)?.status;
  if (status === 403 || status === 404) {
    return true;
  }
  return String(error).toLowerCase().includes("cachedcontent");
}

/**
 * Report token usage through the per-call sink (#478; Python `_gemini_usage`).
 * Skipped entirely when the response carries no usage fields (Python's None return).
 */
function reportGeminiUsage(
  response: GeminiGenerateContentResponse,
  params: StructuredCallParams,
  model: string,
): void {
  if (params.usageSink === undefined) {
    return;
  }
  // `== null`: a JSON-deserialized transport response may carry usageMetadata: null.
  const usage = response.usageMetadata;
  if (usage == null) {
    return;
  }
  if (
    usage.promptTokenCount === undefined &&
    usage.candidatesTokenCount === undefined &&
    usage.totalTokenCount === undefined &&
    usage.cachedContentTokenCount === undefined
  ) {
    return;
  }
  const report: ProviderUsage = { model };
  if (usage.promptTokenCount !== undefined) {
    report.inputTokens = usage.promptTokenCount;
  }
  if (usage.candidatesTokenCount !== undefined) {
    report.outputTokens = usage.candidatesTokenCount;
  }
  if (usage.totalTokenCount !== undefined) {
    // Includes thinking tokens for reasoning models — forwarded verbatim, not derived.
    report.totalTokens = usage.totalTokenCount;
  }
  if (usage.cachedContentTokenCount !== undefined) {
    report.cacheReadTokens = usage.cachedContentTokenCount;
  }
  params.usageSink(report);
}

/** Concatenate a candidate's text parts and parse the JSON. */
function extractStructuredOutput(candidate: GeminiCandidate | undefined): unknown {
  const parts = candidate?.content?.parts;
  if (parts === undefined || parts.length === 0) {
    throw new Error("GeminiProvider: the response contained no content parts");
  }
  const text = parts.map((part) => part.text ?? "").join("");
  try {
    return JSON.parse(text) as unknown;
  } catch (error) {
    throw new Error(`GeminiProvider: the response content was not valid JSON: ${(error as Error).message}`);
  }
}

/**
 * Map a message's content to Gemini native parts (parity with Python `_to_gemini_parts`):
 * a string becomes one text part; a `text` part becomes `{ text }`; a `provider_extension`
 * addressed to Gemini contributes its payload verbatim; `artifact`/`artifact_group` parts
 * resolve via the supplied groups (their optional `text` becomes a text part first) into
 * native `inlineData`/`fileData` parts (#481).
 *
 * Fails loud rather than silently dropping content that can't be represented — a foreign
 * provider_extension, an unknown group, or an artifact the inline path can't carry (#503).
 */
async function contentToParts(
  content: ChatContent,
  artifacts: readonly ResolvedArtifactGroup[],
  uploaded?: ReadonlyMap<string, GeminiFile>,
): Promise<GeminiRequestPart[]> {
  if (typeof content === "string") {
    return [{ text: content }];
  }
  const parts: GeminiRequestPart[] = [];
  for (const part of content) {
    switch (part.type) {
      case "text":
        parts.push({ text: part.text });
        break;
      case "provider_extension":
        if (part.provider !== "gemini") {
          throw new ProviderConfigError(
            `GeminiProvider: content part provider extension ${JSON.stringify(part.provider)} is not for Gemini`,
            { provider: "gemini" },
          );
        }
        // Copy the payload so the caller's part is never mutated downstream.
        parts.push({ ...part.payload });
        break;
      case "artifact_group": {
        if (part.text !== undefined) {
          parts.push({ text: part.text });
        }
        for (const artifact of artifactsForGroup(artifacts, part.group, "gemini")) {
          parts.push(await toGeminiArtifactPart(artifact, uploaded));
        }
        break;
      }
      case "artifact": {
        if (part.text !== undefined) {
          parts.push({ text: part.text });
        }
        parts.push(await toGeminiArtifactPart(artifactForName(artifacts, part.artifact, "gemini"), uploaded));
        break;
      }
      default:
        // Both a compile-time exhaustiveness check (a new ContentPart variant makes `part`
        // non-`never` and fails to compile) AND a runtime guard: an unknown `type` can reach
        // here because the YAML loader validates content parts only as `{ type: string }`.
        // Fail loud (parity with Python's `ProviderConfigError`) rather than returning a
        // malformed non-array part.
        return unsupportedContentPart(part);
    }
  }
  return parts;
}

const SUPPORTED_IMAGE_MEDIA_TYPES = new Set(["image/gif", "image/jpeg", "image/png", "image/webp"]);
const GEMINI_INLINE_MAX_BYTES = 20 * 1024 * 1024;

/**
 * Classify a Gemini transport failure (Python `_classify_gemini_error` parity, applied to BOTH
 * `generateContent` and the Files API): retryable statuses -> `ProviderTransientError`; a
 * non-auth 4xx -> typed non-retryable `ProviderConfigError` (401/403 stay untyped until an auth
 * error class exists); anything else returns unchanged.
 */
function classifyGeminiTransportError(error: unknown, cachedContent?: string): unknown {
  // Stale-cache check FIRST (Python _raise_gemini_provider_error order): a failure on a
  // call that referenced a cachedContent classifies as ProviderCacheUnavailableError so
  // the map wrapper (#478 PR6) can re-run the item once uncached.
  if (cachedContent !== undefined && isStaleCacheError(error)) {
    const status = (error as { status?: unknown } | null)?.status;
    return new ProviderCacheUnavailableError(
      "GeminiProvider: cached content is unavailable (expired or deleted)",
      {
        provider: "gemini",
        ...(typeof status === "number" ? { statusCode: status } : {}),
        cause: error,
      },
    );
  }
  if (isRateLimitTransportError(error)) {
    const hint = retryAfterSecondsFrom(error);
    return new ProviderRateLimitError("GeminiProvider: provider rate limit (status 429)", {
      ...(hint !== undefined ? { retryAfterSeconds: hint } : {}),
      cause: error,
    });
  }
  if (isRetryableTransportError(error)) {
    const status = (error as { status?: number }).status;
    const hint = retryAfterSecondsFrom(error);
    return new ProviderTransientError(`GeminiProvider: transient provider error (status ${status})`, {
      // A non-429 transient (5xx) can carry Retry-After too — Python attaches
      // the hint at every transient wrap site.
      ...(hint !== undefined ? { retryAfterSeconds: hint } : {}),
      cause: error,
    });
  }
  const rejectedStatus = (error as { status?: unknown } | null)?.status;
  if (typeof rejectedStatus === "number" && rejectedStatus >= 400 && rejectedStatus < 500 && rejectedStatus !== 401 && rejectedStatus !== 403) {
    return new ProviderConfigError(`GeminiProvider: the provider rejected the request (status ${rejectedStatus})`, {
      provider: "gemini",
      cause: error,
    });
  }
  return error;
}

/** The per-request upload dedup key: content identity when available, else the resolved path. */
function artifactUploadKey(artifact: ResolvedArtifact): string {
  return artifact.sha256 ?? String(artifact.local_path);
}

/** Whether a local artifact must ride the Files API (Python `_artifact_needs_files_api` + stat fallback). */
async function artifactNeedsFilesApi(artifact: ResolvedArtifact): Promise<boolean> {
  if (artifact.local_path === undefined) {
    return false;
  }
  // Python falls back to stat() when the resolver did not record a size — a missing size_bytes
  // must not silently inline an oversize payload for an opaque provider 400.
  const sizeBytes = artifact.size_bytes ?? (await stat(artifact.local_path)).size;
  return sizeBytes > GEMINI_INLINE_MAX_BYTES;
}

/**
 * One resolved artifact as a Gemini-native part (Python `_to_gemini_artifact_part`): a local
 * artifact uploaded via the Files API (#503, oversize) is referenced by uri; otherwise the inline
 * path — local files under 20MB as base64 `inlineData` (images restricted to gif/jpeg/png/webp),
 * URL sources as `fileData` references.
 */
async function toGeminiArtifactPart(
  artifact: ResolvedArtifact,
  uploaded?: ReadonlyMap<string, GeminiFile>,
): Promise<GeminiRequestPart> {
  if (uploaded !== undefined && artifact.local_path !== undefined) {
    const file = uploaded.get(artifactUploadKey(artifact));
    if (file !== undefined) {
      // Python truthiness (`artifact.media_type or file.mime_type`): "" counts as missing.
      const mimeType = (artifact.media_type ? artifact.media_type : undefined) ?? file.mimeType;
      if (mimeType === undefined) {
        // Parity with the inline path, which also requires a media type.
        throw new ProviderConfigError(
          "GeminiProvider cannot reference an uploaded artifact without a media type",
          { provider: "gemini" },
        );
      }
      return { fileData: { fileUri: file.uri, mimeType } };
    }
  }
  const source = artifact.ref.source;
  if (await artifactNeedsFilesApi(artifact)) {
    // Invariant backstop (Python #358 guard): an oversize local artifact must have been routed
    // through uploadOversizeArtifacts before reaching the inline builder.
    throw new ProviderConfigError(
      "GeminiProvider reached the inline path for an artifact over the inline size cap; " +
        "it must be uploaded via the Files API first",
      { provider: "gemini" },
    );
  }
  const mediaTypePrefix = artifact.media_type ?? "";
  const isImage = artifact.kind === "image" || mediaTypePrefix.startsWith("image/");
  const isAudioOrVideo =
    artifact.kind === "audio" ||
    artifact.kind === "video" ||
    mediaTypePrefix.startsWith("audio/") ||
    mediaTypePrefix.startsWith("video/");
  if (source.type === "local_path" && artifact.local_path !== undefined) {
    // Python truthiness (`media_type or ...`): an empty string counts as missing.
    const mediaType = (artifact.media_type !== undefined && artifact.media_type !== "" ? artifact.media_type : undefined) ?? (isImage ? "image/png" : undefined);
    if (isImage && (mediaType === undefined || !SUPPORTED_IMAGE_MEDIA_TYPES.has(mediaType))) {
      throw new ProviderConfigError(
        `GeminiProvider does not support image media type ${JSON.stringify(mediaType)}`,
        { provider: "gemini" },
      );
    }
    if (mediaType === undefined) {
      throw new ProviderConfigError("GeminiProvider cannot attach an artifact without a media type", {
        provider: "gemini",
      });
    }
    // The JS SDK's inlineData carries base64 (Python's google-genai takes raw bytes).
    const data = (await readFile(artifact.local_path)).toString("base64");
    return { inlineData: { mimeType: mediaType, data } };
  }
  if (source.type === "url") {
    if (artifact.media_type === undefined || artifact.media_type === "") {
      throw new ProviderConfigError("GeminiProvider needs a media type to attach a URL artifact", {
        provider: "gemini",
      });
    }
    if (isAudioOrVideo) {
      // Parity with Python: the Files API uploads LOCAL files only, so URL-sourced audio/video
      // is unsupported in both SDKs — supply it as a local file. Fail loud rather than silently
      // sending it down the image/document fileData route.
      throw new ProviderConfigError(
        "GeminiProvider attaches audio/video from local files only (inline or Files API); " +
          "URL-sourced audio/video is not supported — supply a local_path source",
        { provider: "gemini" },
      );
    }
    return { fileData: { fileUri: source.url, mimeType: artifact.media_type } };
  }
  throw new ProviderConfigError("GeminiProvider cannot attach this artifact through the inline path", {
    provider: "gemini",
  });
}

/** Throw on a content part whose `type` is not one this provider maps (see `contentToParts`). */
function unsupportedContentPart(part: never): never {
  const type = (part as { type?: unknown }).type;
  throw new ProviderConfigError(`GeminiProvider: unsupported content part type ${JSON.stringify(type)}`, { provider: "gemini" });
}

const GEMINI_FILE_ACTIVE_TIMEOUT_MS = 120_000;
const GEMINI_FILE_POLL_INTERVAL_MS = 2_000;

/**
 * The resolved artifacts actually referenced by the messages' artifact parts (Python
 * `_gemini_referenced_artifacts`): a pure-text call must never touch the Files API or pay for
 * unsent data. Dedup is by object identity, mirroring what the part builder will resolve.
 */
function referencedArtifacts(
  messages: StructuredCallParams["messages"],
  artifacts: readonly ResolvedArtifactGroup[],
): ResolvedArtifact[] {
  const referenced: ResolvedArtifact[] = [];
  const seen = new Set<ResolvedArtifact>();
  const add = (artifact: ResolvedArtifact): void => {
    if (!seen.has(artifact)) {
      seen.add(artifact);
      referenced.push(artifact);
    }
  };
  for (const message of messages) {
    if (typeof message.content === "string") {
      continue;
    }
    for (const part of message.content) {
      if (part.type === "artifact_group") {
        for (const artifact of artifactsForGroup(artifacts, part.group, "gemini")) {
          add(artifact);
        }
      } else if (part.type === "artifact") {
        add(artifactForName(artifacts, part.artifact, "gemini"));
      }
    }
  }
  return referenced;
}

/**
 * Upload each REFERENCED local artifact over the inline cap via the Files API, once per
 * {@link artifactUploadKey}, returning the map the part builder references by uri (Python
 * `_upload_oversize_artifacts`). Fails loud and typed when the transport has no `files` surface,
 * on a FAILED upload, on a poll timeout, or on a missing `uri`.
 */
async function uploadOversizeArtifacts(
  transport: GeminiGenerateContentTransport,
  messages: StructuredCallParams["messages"],
  artifacts: readonly ResolvedArtifactGroup[],
  signal: AbortSignal | undefined,
): Promise<ReadonlyMap<string, GeminiFile>> {
  const uploaded = new Map<string, GeminiFile>();
  if (artifacts.length === 0) {
    return uploaded;
  }
  for (const artifact of referencedArtifacts(messages, artifacts)) {
    if (!(await artifactNeedsFilesApi(artifact))) {
      continue;
    }
    const key = artifactUploadKey(artifact);
    if (uploaded.has(key)) {
      continue;
    }
    if (transport.files === undefined) {
      throw new ProviderConfigError(
        "GeminiProvider: this artifact is over the 20MB inline cap and the transport has no " +
          "`files` surface — wire the Files API on the transport adapter (#503)",
        { provider: "gemini" },
      );
    }
    let file: GeminiFile;
    try {
      // Python truthiness (`if artifact.media_type`): "" counts as missing.
      file = await transport.files.upload({
        file: String(artifact.local_path),
        ...(artifact.media_type ? { config: { mimeType: artifact.media_type } } : {}),
      });
    } catch (error) {
      throw classifyGeminiTransportError(error);
    }
    const active = await awaitFileActive(transport.files, file, signal);
    if (active.uri === undefined || active.uri === "") {
      throw new ProviderConfigError(`Gemini Files API returned no uri for ${JSON.stringify(key)}`, {
        provider: "gemini",
      });
    }
    uploaded.set(key, active);
  }
  return uploaded;
}

/**
 * Poll a Files API upload until ACTIVE (Python `_await_gemini_file_active`): images/PDFs/audio
 * upload ACTIVE immediately; video may sit in PROCESSING while it transcodes. Bounded at 120s,
 * failing loud on FAILED or timeout rather than referencing an unusable file. The poll sleep
 * honors the activity's cancellation signal (#487) so a cancelled workflow does not wait out
 * a transcode.
 */
async function awaitFileActive(
  files: GeminiFilesTransport,
  file: GeminiFile,
  signal: AbortSignal | undefined,
): Promise<GeminiFile> {
  const deadline = Date.now() + GEMINI_FILE_ACTIVE_TIMEOUT_MS;
  // A function read defeats TS narrowing: `aborted` can flip DURING the awaits below.
  const abortedNow = (): boolean => signal?.aborted === true;
  const abortReason = (): Error =>
    signal?.reason instanceof Error ? signal.reason : new Error("aborted while awaiting Gemini file");
  let current = file;
  while ((current.state ?? "") === "PROCESSING") {
    if (abortedNow()) {
      throw abortReason();
    }
    if (Date.now() > deadline) {
      throw new ProviderConfigError(
        `Gemini file ${JSON.stringify(current.name ?? "?")} did not become ACTIVE within ${GEMINI_FILE_ACTIVE_TIMEOUT_MS / 1000}s`,
        { provider: "gemini" },
      );
    }
    await abortableDelay(GEMINI_FILE_POLL_INTERVAL_MS, signal);
    if (abortedNow()) {
      // The abort ended the sleep early — do not issue another Files API call for a
      // cancelling activity.
      throw abortReason();
    }
    try {
      current = await files.get({ name: current.name ?? "" });
    } catch (error) {
      throw classifyGeminiTransportError(error);
    }
  }
  if ((current.state ?? "") === "FAILED") {
    throw new ProviderConfigError(`Gemini file upload failed: ${JSON.stringify(current.name ?? "?")}`, {
      provider: "gemini",
    });
  }
  return current;
}
