/**
 * An Anthropic `ModelProvider` (Epic 2, #449). Sends the activity's provider-safe
 * output schema via Anthropic's structured outputs (`output_config.format.json_schema`)
 * and returns the parsed JSON (the executor validates it with Zod).
 *
 * The Anthropic client is injected as a thin structural transport, so no
 * `@anthropic-ai/sdk` dependency is added here — wire a real client with a one-line
 * adapter rather than passing it directly:
 *
 *     new AnthropicProvider({ messages: {
 *       create: (request, opts) =>
 *         client.messages.create(request, { signal: opts?.signal, timeout: opts?.timeoutMs }),
 *     } }, { model });
 *
 * The real SDK's `messages.parse(...)` (structured outputs) also fits this transport
 * — it returns the same shape with `parsed_output` populated. Tests pass a fake
 * transport; any Anthropic-compatible endpoint works.
 */

import { readFile } from "node:fs/promises";

import { artifactForName, artifactsForGroup, type ResolvedArtifact, type ResolvedArtifactGroup } from "./artifacts.js";
import type { ChatContent } from "./content-parts.js";
import {
  numberProviderParam,
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
// Surfaced as a `ProviderTransientError` so `executeActivity`'s default classifier retries.
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

/** The Messages request this provider builds (a subset of Anthropic's). */
export interface AnthropicMessagesRequest {
  model: string;
  max_tokens: number;
  /** Behavior params (#495) — set only when the merged call params carry them. */
  temperature?: number;
  top_p?: number;
  top_k?: number;
  stop_sequences?: string[];
  /**
   * Anthropic's system prompt is a top-level field, not a message role. A block
   * list is how `cache_control` attaches (#478) — used when a session cache is
   * engaged; a plain string otherwise.
   */
  system?: string | Record<string, unknown>[];
  messages: { role: string; content: string | Record<string, unknown>[] }[];
  output_config: {
    format: { type: "json_schema"; schema: JsonSchema };
  };
}

/** One response content block (a subset — `text` for structured output, `parsed_output` from `.parse()`). */
export interface AnthropicContentBlock {
  type: string;
  text?: string;
  parsed_output?: unknown;
}

/** The Messages response this provider reads (a subset of Anthropic's). */
export interface AnthropicMessagesResponse {
  content: AnthropicContentBlock[];
  stop_reason?: string | null;
  /** Present when the request went through the SDK's `messages.parse(...)`. */
  parsed_output?: unknown;
  /**
   * Token accounting (#478). Anthropic's `input_tokens` is the UNCACHED input;
   * the prompt-cache fields are separate signals (read = served from cache,
   * creation = written this call).
   */
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
  };
}

/** The minimal Anthropic-style client slice the provider needs (wire a real client via a thin adapter). */
export interface AnthropicMessagesTransport {
  messages: {
    create(request: AnthropicMessagesRequest, options?: TransportCallOptions): Promise<AnthropicMessagesResponse>;
  };
}

export interface AnthropicProviderOptions {
  /** Default model when a call doesn't set one. For cross-run cache correctness, pass
   * the effective model via `executeActivity`'s options (it enters the cache key). */
  model?: string;
  /** `max_tokens` for the response (required by Anthropic; default `4096`). */
  maxTokens?: number;
}

const DEFAULT_MAX_TOKENS = 4096;

export class AnthropicProvider implements ModelProvider {
  readonly providerName = "anthropic";
  /** Session-cache capability (#478): prefix style — `cache_control` breakpoints per call. */
  readonly supportsSessionCache = true;
  readonly sessionCacheStyle = "prefix" as const;
  private readonly transport: AnthropicMessagesTransport;
  private readonly defaultModel: string | undefined;
  private readonly maxTokens: number;

  constructor(transport: AnthropicMessagesTransport, options: AnthropicProviderOptions = {}) {
    this.transport = transport;
    this.defaultModel = options.model;
    this.maxTokens = options.maxTokens ?? DEFAULT_MAX_TOKENS;
    if (this.maxTokens < 1) {
      throw new ProviderConfigError("AnthropicProvider: maxTokens must be >= 1", { provider: "anthropic" });
    }
  }

  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    const model = params.model ?? this.defaultModel;
    if (model === undefined) {
      throw new ProviderConfigError("AnthropicProvider: a model is required (set it on the call or via options.model)", {
        provider: "anthropic",
      });
    }

    // Anthropic's system prompt is a top-level field — pull system-role messages out of
    // the message list and join them, leaving user/assistant turns as the messages.
    const artifacts = params.artifacts ?? [];
    const systemParts: string[] = [];
    const messages: { role: string; content: string | Record<string, unknown>[] }[] = [];
    for (const message of params.messages) {
      if (message.role === "system") {
        systemParts.push(systemContentToText(message.content));
        continue;
      }
      if (message.role !== "user" && message.role !== "assistant") {
        // Parity with Python's Anthropic provider (and the Gemini guard): fail locally and typed
        // rather than sending an unsupported role to the API for an opaque 400.
        throw new ProviderConfigError(`AnthropicProvider: unsupported message role ${JSON.stringify(message.role)}`, {
          provider: "anthropic",
        });
      }
      messages.push({
        role: message.role,
        content:
          typeof message.content === "string"
            ? message.content
            : await contentToAnthropicParts(message.content, artifacts),
      });
    }

    // Behavior params (#495; Python _anthropic_call_kwargs): forwarded only when present
    // and well-typed; a params max_tokens overrides the constructor cap.
    const behavior = params.providerParams;
    const temperature = numberProviderParam(behavior, "temperature");
    const topP = numberProviderParam(behavior, "top_p");
    const topK = numberProviderParam(behavior, "top_k");
    const stop = stopProviderParam(behavior);
    const request: AnthropicMessagesRequest = {
      model,
      max_tokens: numberProviderParam(behavior, "max_tokens") ?? this.maxTokens,
      ...(temperature !== undefined ? { temperature } : {}),
      ...(topP !== undefined ? { top_p: topP } : {}),
      ...(topK !== undefined ? { top_k: topK } : {}),
      ...(stop !== undefined ? { stop_sequences: stop } : {}),
      messages,
      output_config: { format: { type: "json_schema", schema: params.outputSchema } },
    };
    // Python `_should_cache_prefix`: mark breakpoints only for an ENGAGED session
    // (a fail-soft handle sends full context with no cache_control, unchanged).
    const cachePrefix = params.cachedSession != null && params.cachedSession.supported;
    if (systemParts.length > 0) {
      const systemText = systemParts.join("\n\n");
      // A list-of-blocks system is how Anthropic attaches cache_control; the
      // stable instructions are the cheapest always-stable thing to cache.
      request.system = cachePrefix
        ? [{ type: "text", text: systemText, cache_control: { type: "ephemeral" } }]
        : systemText;
    }
    if (cachePrefix) {
      markPrefixCacheBreakpoint(
        messages,
        params.cachedSession?.prefix_stable_messages ?? null,
        params.cachedSession?.per_item_artifact_messages ?? false,
      );
    }

    let response: AnthropicMessagesResponse;
    try {
      response = await this.transport.messages.create(request, callOptions(params));
    } catch (error) {
      if (isRateLimitTransportError(error)) {
        const hint = retryAfterSecondsFrom(error);
        throw new ProviderRateLimitError("AnthropicProvider: provider rate limit (status 429)", {
          ...(hint !== undefined ? { retryAfterSeconds: hint } : {}),
          cause: error,
        });
      }
      if (isRetryableTransportError(error)) {
        const status = (error as { status?: number }).status;
        const hint = retryAfterSecondsFrom(error);
        throw new ProviderTransientError(`AnthropicProvider: transient provider error (status ${status})`, {
          // A non-429 transient (503/529 overloaded) can carry Retry-After too —
          // Python attaches the hint at every transient wrap site.
          ...(hint !== undefined ? { retryAfterSeconds: hint } : {}),
          cause: error,
        });
      }
      // Python parity (_classify_provider_error): a non-auth 4xx is a provider-side CONFIG
      // rejection (bad request, unknown model, unprocessable schema) — typed and non-retryable.
      // 401/403 stay untyped until an auth error class exists; 408/409/429 were transient above.
      const rejectedStatus = (error as { status?: unknown } | null)?.status;
      if (typeof rejectedStatus === "number" && rejectedStatus >= 400 && rejectedStatus < 500 && rejectedStatus !== 401 && rejectedStatus !== 403) {
        throw new ProviderConfigError(`AnthropicProvider: the provider rejected the request (status ${rejectedStatus})`, {
          provider: "anthropic",
          cause: error,
        });
      }
      throw error;
    }

    raiseIfTruncated(response.stop_reason === "max_tokens", {
      provider: "anthropic",
      message: "AnthropicProvider: the response was truncated (stop_reason max_tokens); increase maxTokens",
    });
    reportAnthropicUsage(response, params, model);
    return extractStructuredOutput(response);
  }

  /**
   * Prefix-style prep (Python parity): there is no server-side cache object to
   * create up front — the prefix is re-sent and marked `cache_control: ephemeral`
   * on each call, so this returns an engaged handle with NO API call and no
   * `cache_id`. `created_at` is left for the prep activity to stamp (replay-safe);
   * `ttl_seconds` stays null — Anthropic's ephemeral cache uses its own default
   * (~5m) and the requested TTL is never sent, so none is recorded.
   * `params.providerParams` is identity-only in TS (no ProviderParams merge,
   * #495): the handle records `params.model ?? default`, exactly the model the
   * fan-out calls will use.
   */
  prepareCachedSession(params: PrepareCachedSessionParams): CachedSessionHandle {
    return {
      provider: this.providerName,
      identity_hash: params.identityHash,
      supported: true,
      style: "prefix",
      cache_id: null,
      model: params.model ?? this.defaultModel ?? null,
      created_at: null,
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    };
  }

  /** No-op: Anthropic's ephemeral prompt cache is TTL-managed with no delete API. */
  releaseCachedSession(): void {}
}

/** Anthropic content-block types that accept a `cache_control` breakpoint. */
const ANTHROPIC_CACHEABLE_BLOCK_TYPES = new Set(["text", "image", "document", "tool_use", "tool_result"]);

/**
 * Mark the end of the stable prefix so Anthropic caches up to and including it
 * (Python `_mark_prefix_cache_breakpoint`). The runtime passes
 * `[stable prefix..., per-item input]` as the conversation (prefix style), so the
 * breakpoint goes on the last CACHEABLE block of the second-to-last message — the
 * final message is the variable per-item input. With a single message (no separate
 * prefix turn) the system breakpoint already covers the stable part, so there is
 * nothing to mark. A trailing non-cacheable block (e.g. a provider-extension part)
 * is skipped — Anthropic rejects `cache_control` on it.
 *
 * `stableMessages` (#362): when reference artifacts lead the conversation the
 * handle carries their turn count `k`; the breakpoint then lands on the last
 * stable turn `conversation[k - 1]` so the documents join the cached prefix. The
 * final (variable) message is never marked — if `k` would reach or exceed it,
 * clamp back to the legacy `conversation[-2]` contract. `k` null/0 keeps that
 * legacy contract byte-for-byte.
 *
 * `perItemArtifactMessages` (#698): with no leading reference span (`k` null/0) but
 * per-item artifact turns trailing the varying input — the shape
 * `[system, per-item query, per-item artifact]` — `conversation[-2]` is the varying
 * query, not a stable turn. Marking it would key the conversation cache on content
 * that differs per item, so mark NOTHING in the conversation: the shape has no
 * stable conversation span (only the system block caches). Ignored when `k` > 0
 * (the reference span is the authoritative breakpoint).
 */
function markPrefixCacheBreakpoint(
  conversation: { role: string; content: string | Record<string, unknown>[] }[],
  stableMessages: number | null = null,
  perItemArtifactMessages = false,
): void {
  if (conversation.length < 2) {
    return;
  }
  if (perItemArtifactMessages && (stableMessages === null || stableMessages <= 0)) {
    // No stable conversation span to cache — the trailing per-item artifact means
    // the last two turns both vary. Leave the conversation unmarked (#698).
    return;
  }
  const index =
    stableMessages !== null && stableMessages > 0
      ? Math.min(stableMessages - 1, conversation.length - 2)
      : conversation.length - 2;
  const prefixMessage = conversation[index] as (typeof conversation)[number];
  const content = prefixMessage.content;
  if (typeof content === "string") {
    prefixMessage.content = [{ type: "text", text: content, cache_control: { type: "ephemeral" } }];
    return;
  }
  for (let index = content.length - 1; index >= 0; index -= 1) {
    const block = content[index];
    if (block !== undefined && typeof block["type"] === "string" && ANTHROPIC_CACHEABLE_BLOCK_TYPES.has(block["type"])) {
      content[index] = { ...block, cache_control: { type: "ephemeral" } };
      return;
    }
  }
}

/**
 * Report token usage through the per-call sink (#478; Python `_anthropic_usage`).
 * Skipped entirely when the response carries no usage fields (Python's None return).
 */
function reportAnthropicUsage(
  response: AnthropicMessagesResponse,
  params: StructuredCallParams,
  model: string,
): void {
  if (params.usageSink === undefined) {
    return;
  }
  const usage = response.usage;
  if (usage === undefined) {
    return;
  }
  if (
    usage.input_tokens === undefined &&
    usage.output_tokens === undefined &&
    usage.cache_read_input_tokens === undefined &&
    usage.cache_creation_input_tokens === undefined
  ) {
    return;
  }
  const report: ProviderUsage = { model };
  if (usage.input_tokens !== undefined) {
    report.inputTokens = usage.input_tokens;
  }
  if (usage.output_tokens !== undefined) {
    report.outputTokens = usage.output_tokens;
  }
  if (usage.cache_read_input_tokens !== undefined) {
    report.cacheReadTokens = usage.cache_read_input_tokens;
  }
  if (usage.cache_creation_input_tokens !== undefined) {
    report.cacheWriteTokens = usage.cache_creation_input_tokens;
  }
  params.usageSink(report);
}

/** Pull the structured object out of a response — `parsed_output` (SDK `.parse()`) else the JSON text block. */
function extractStructuredOutput(response: AnthropicMessagesResponse): unknown {
  // `!= null` (not undefined AND not null): a `parsed_output` of `null` means "not
  // parsed" — fall through to the JSON text block rather than returning `null`
  // (parity with Python, which ignores a null parsed_output).
  if (response.parsed_output != null) {
    return response.parsed_output;
  }
  for (const block of response.content) {
    if (block.parsed_output != null) {
      return block.parsed_output;
    }
  }
  for (const block of response.content) {
    if (block.type === "text" && typeof block.text === "string") {
      try {
        return JSON.parse(block.text) as unknown;
      } catch (error) {
        throw new Error(`AnthropicProvider: the response content was not valid JSON: ${(error as Error).message}`);
      }
    }
  }
  throw new Error("AnthropicProvider: the response contained no structured output");
}

/**
 * A system message's content as text — a string passes through; for parts, every
 * text-bearing part contributes its `text` (text / artifact / artifact-group parts —
 * the same fields `renderContentParts` renders), so a rendered system prompt does not
 * silently lose instructions. Provider-extension parts carry no text.
 */
/**
 * A system message's content as text (Python `_to_anthropic_system_text`): Anthropic's system
 * prompt is a plain string, so only text — a text part, or an artifact part's preamble text —
 * is representable; an artifact with no text (or any other part) fails loud rather than being
 * silently dropped from the system prompt.
 */
function systemContentToText(content: ChatContent): string {
  if (typeof content === "string") {
    return content;
  }
  const textParts: string[] = [];
  for (const part of content) {
    if (part.type === "text") {
      textParts.push(part.text);
      continue;
    }
    if ((part.type === "artifact" || part.type === "artifact_group") && part.text !== undefined) {
      textParts.push(part.text);
      continue;
    }
    throw new ProviderConfigError("AnthropicProvider only supports text content in system messages", {
      provider: "anthropic",
    });
  }
  return textParts.join("\n\n");
}

const SUPPORTED_IMAGE_MEDIA_TYPES = new Set(["image/gif", "image/jpeg", "image/png", "image/webp"]);

/**
 * Map typed content parts to Anthropic-native blocks (Python `_to_anthropic_message_content`):
 * text passes through; artifact / artifact_group parts resolve via the supplied groups (their
 * optional `text` first); an `anthropic` provider_extension contributes its payload verbatim;
 * anything else fails loud and typed.
 */
async function contentToAnthropicParts(
  parts: Exclude<ChatContent, string>,
  artifacts: readonly ResolvedArtifactGroup[],
): Promise<Record<string, unknown>[]> {
  const payload: Record<string, unknown>[] = [];
  for (const part of parts) {
    switch (part.type) {
      case "text":
        payload.push({ type: "text", text: part.text });
        break;
      case "artifact_group": {
        if (part.text !== undefined) {
          payload.push({ type: "text", text: part.text });
        }
        for (const artifact of artifactsForGroup(artifacts, part.group, "anthropic")) {
          payload.push(await toAnthropicArtifactPart(artifact));
        }
        break;
      }
      case "artifact": {
        if (part.text !== undefined) {
          payload.push({ type: "text", text: part.text });
        }
        payload.push(await toAnthropicArtifactPart(artifactForName(artifacts, part.artifact, "anthropic")));
        break;
      }
      case "provider_extension":
        if (part.provider !== "anthropic") {
          throw new ProviderConfigError(
            `content part provider extension ${JSON.stringify(part.provider)} is not for Anthropic`,
            { provider: "anthropic" },
          );
        }
        payload.push({ ...part.payload });
        break;
      default: {
        const unreachable: never = part;
        throw new ProviderConfigError(
          `unsupported Anthropic content part: ${JSON.stringify((unreachable as { type?: unknown }).type)}`,
          { provider: "anthropic" },
        );
      }
    }
  }
  return payload;
}

/**
 * One resolved artifact as an Anthropic-native block (Python `_to_anthropic_artifact_part`):
 * images as `image` blocks (URL, or base64 restricted to gif/jpeg/png/webp), text-like local
 * files inlined as text, local PDFs as base64 `document` blocks, Anthropic provider files by id —
 * anything else cannot ride the messages path and fails loud.
 */
async function toAnthropicArtifactPart(artifact: ResolvedArtifact): Promise<Record<string, unknown>> {
  const source = artifact.ref.source;
  const isImage = artifact.kind === "image" || (artifact.media_type ?? "").startsWith("image/");
  if (isImage) {
    if (source.type === "url") {
      return { type: "image", source: { type: "url", url: source.url } };
    }
    if (source.type === "local_path" && artifact.local_path !== undefined) {
      // Python truthiness (`media_type or "image/png"`): an empty string counts as missing.
      const mediaType = artifact.media_type !== undefined && artifact.media_type !== "" ? artifact.media_type : "image/png";
      if (!SUPPORTED_IMAGE_MEDIA_TYPES.has(mediaType)) {
        throw new ProviderConfigError(
          `AnthropicProvider does not support image media type ${JSON.stringify(mediaType)}`,
          { provider: "anthropic" },
        );
      }
      const data = (await readFile(artifact.local_path)).toString("base64");
      return { type: "image", source: { type: "base64", media_type: mediaType, data } };
    }
  }
  if (source.type === "local_path" && artifact.local_path !== undefined) {
    if (isTextLikeMediaType(artifact.media_type)) {
      return { type: "text", text: await readFile(artifact.local_path, "utf-8") };
    }
    if (artifact.media_type === "application/pdf") {
      const data = (await readFile(artifact.local_path)).toString("base64");
      return { type: "document", source: { type: "base64", media_type: "application/pdf", data } };
    }
  }
  if (source.type === "provider_file") {
    if (source.provider !== "anthropic") {
      throw new ProviderConfigError(`provider file ${JSON.stringify(source.provider)} is not for Anthropic`, {
        provider: "anthropic",
      });
    }
    return { type: isImage ? "image" : "document", source: { type: "file", file_id: source.file_id } };
  }
  throw new ProviderConfigError("AnthropicProvider cannot attach this artifact through the messages path", {
    provider: "anthropic",
  });
}

function isTextLikeMediaType(mediaType: string | undefined): boolean {
  if (mediaType === undefined) {
    return false;
  }
  // Python's Anthropic helper (unlike its OpenAI one) normalizes parameters/aliases:
  // `text/plain; charset=utf-8` and `application/csv` count as text-like.
  const normalized = (mediaType.split(";", 1)[0] ?? "").trim().toLowerCase();
  return (
    normalized.startsWith("text/") ||
    ["application/csv", "application/json", "application/xml", "application/x-ndjson", "text/csv"].includes(normalized)
  );
}
