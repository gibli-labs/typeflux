/**
 * An OpenAI-compatible `ModelProvider` (#430). Sends the activity's provider-safe
 * output schema as a strict `json_schema` `response_format` and returns the parsed
 * JSON content (the executor validates it with Zod).
 *
 * The OpenAI client is injected as a thin structural transport, so no `openai`
 * dependency is added here. The SDK's own request types are broader than this
 * transport needs (e.g. it types message roles as a literal union), so wire a real
 * client with a one-line adapter rather than passing it directly:
 *
 *     new OpenAIProvider({ chat: { completions: {
 *       create: (request, opts) =>
 *         client.chat.completions.create(request, { signal: opts?.signal, timeout: opts?.timeoutMs }),
 *     } } }, { model });
 *
 * Tests pass a fake transport; any OpenAI-compatible endpoint works.
 */

import { readFile } from "node:fs/promises";
import { basename } from "node:path";

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
import type { JsonSchema } from "./provider-schema.js";
import type { CachedSessionHandle } from "./session-cache.js";

// HTTP statuses worth retrying — rate limit, request timeout, conflict, and any
// 5xx — the same set the OpenAI SDK itself retries. Surfaced as a
// `ProviderTransientError` so `executeActivity`'s default classifier retries them.
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

/** The chat-completions request this provider builds (a subset of OpenAI's). */
export interface ChatCompletionRequest {
  model: string;
  // String content maps directly to an OpenAI request; typed SDK content parts are mapped
  // to OpenAI-native parts (text / image_url / file, #481) BEFORE the transport sees them —
  // an adapter always receives OpenAI's own content-array shape, never SDK ContentParts.
  messages: { role: string; content: string | Record<string, unknown>[]; name?: string }[];
  /** Behavior params (#495) — set only when the merged call params carry them. */
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  seed?: number;
  frequency_penalty?: number;
  presence_penalty?: number;
  stop?: string[];
  response_format: {
    type: "json_schema";
    json_schema: { name: string; schema: JsonSchema; strict: boolean };
  };
}

/** The chat-completions response this provider reads (a subset of OpenAI's). */
export interface ChatCompletionResponse {
  choices: { message: { content: string | null }; finish_reason?: string }[];
  /** Token accounting (#478). `prompt_tokens_details.cached_tokens` is the implicit prefix-cache hit. */
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    prompt_tokens_details?: { cached_tokens?: number };
  };
}

/** The minimal OpenAI-style client slice the provider needs (wire a real `openai`
 * client via a thin adapter — its request types are broader; see the module doc). */
export interface OpenAIChatTransport {
  chat: {
    completions: {
      create(request: ChatCompletionRequest, options?: TransportCallOptions): Promise<ChatCompletionResponse>;
    };
  };
}

export interface OpenAIProviderOptions {
  /** Default model when a call doesn't set one. For cross-run cache correctness,
   * pass the effective model via `executeActivity`'s options (it enters the cache
   * key) — this provider default is not visible to the cache. */
  model?: string;
  /** The `json_schema` name sent to the provider (default `"response"`). */
  schemaName?: string;
  /** Send `strict` in the response_format (default `true`); set `false` for
   * OpenAI-compatible endpoints that don't support strict `json_schema`. */
  strict?: boolean;
}

/** A `ModelProvider` backed by an OpenAI-compatible chat-completions transport. */
export class OpenAIProvider implements ModelProvider {
  readonly providerName = "openai";
  /** Session-cache capability (#478): implicit prefix caching, no cache object. */
  readonly supportsSessionCache = true;
  readonly sessionCacheStyle = "prefix" as const;
  private readonly transport: OpenAIChatTransport;
  private readonly defaultModel: string | undefined;
  private readonly schemaName: string;
  private readonly strict: boolean;

  constructor(transport: OpenAIChatTransport, options: OpenAIProviderOptions = {}) {
    this.transport = transport;
    this.defaultModel = options.model;
    this.schemaName = options.schemaName ?? "response";
    this.strict = options.strict ?? true;
  }

  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    const model = params.model ?? this.defaultModel;
    if (model === undefined) {
      throw new ProviderConfigError(
        "OpenAIProvider: a model is required (set it on the call or via options.model)",
        { provider: "openai" },
      );
    }
    const artifacts = params.artifacts ?? [];
    // Behavior params (#495; Python _openai_call_kwargs): forwarded only when present
    // and well-typed — the merged record may carry provider-specific extras we ignore.
    const behavior = params.providerParams;
    const temperature = numberProviderParam(behavior, "temperature");
    const maxTokens = numberProviderParam(behavior, "max_tokens");
    const topP = numberProviderParam(behavior, "top_p");
    const seed = numberProviderParam(behavior, "seed");
    const frequencyPenalty = numberProviderParam(behavior, "frequency_penalty");
    const presencePenalty = numberProviderParam(behavior, "presence_penalty");
    const stop = stopProviderParam(behavior);
    const request: ChatCompletionRequest = {
      model,
      messages: await Promise.all(
        params.messages.map(async (message) => ({
          role: message.role,
          content:
            typeof message.content === "string"
              ? message.content
              : await contentToOpenAIParts(message.content, artifacts),
          ...(message.name !== undefined ? { name: message.name } : {}),
        })),
      ),
      ...(temperature !== undefined ? { temperature } : {}),
      ...(maxTokens !== undefined ? { max_tokens: maxTokens } : {}),
      ...(topP !== undefined ? { top_p: topP } : {}),
      ...(seed !== undefined ? { seed } : {}),
      ...(frequencyPenalty !== undefined ? { frequency_penalty: frequencyPenalty } : {}),
      ...(presencePenalty !== undefined ? { presence_penalty: presencePenalty } : {}),
      ...(stop !== undefined ? { stop } : {}),
      response_format: {
        type: "json_schema" as const,
        json_schema: { name: this.schemaName, schema: params.outputSchema, strict: this.strict },
      },
    };
    let response;
    try {
      response = await this.transport.chat.completions.create(request, callOptions(params));
    } catch (error) {
      if (isRateLimitTransportError(error)) {
        const hint = retryAfterSecondsFrom(error);
        throw new ProviderRateLimitError("OpenAIProvider: provider rate limit (status 429)", {
          ...(hint !== undefined ? { retryAfterSeconds: hint } : {}),
          cause: error,
        });
      }
      // Surface retryable HTTP failures as ProviderTransientError so the executor's
      // default classifier retries them (when `transientRetries` is set).
      if (isRetryableTransportError(error)) {
        const status = (error as { status?: number }).status;
        const hint = retryAfterSecondsFrom(error);
        throw new ProviderTransientError(
          `OpenAIProvider: transient provider error (status ${status})`,
          {
            // A non-429 transient (5xx) can carry Retry-After too — Python
            // attaches the hint at every transient wrap site.
            ...(hint !== undefined ? { retryAfterSeconds: hint } : {}),
            cause: error,
          },
        );
      }
      // Python parity (_classify_provider_error): a non-auth 4xx is a provider-side CONFIG
      // rejection (bad request, unknown model, unprocessable schema) — typed and non-retryable.
      // 401/403 stay untyped until an auth error class exists; 408/409/429 were transient above.
      const rejectedStatus = (error as { status?: unknown } | null)?.status;
      if (typeof rejectedStatus === "number" && rejectedStatus >= 400 && rejectedStatus < 500 && rejectedStatus !== 401 && rejectedStatus !== 403) {
        throw new ProviderConfigError(`OpenAIProvider: the provider rejected the request (status ${rejectedStatus})`, {
          provider: "openai",
          cause: error,
        });
      }
      throw error;
    }
    // Truncation BEFORE usage (Python + the Anthropic guard's ordering, #518): a
    // response cut at the output-token cap is a degraded result, not parseable output —
    // fail loud and report no usage for it.
    raiseIfTruncated(response.choices?.[0]?.finish_reason === "length", {
      provider: "openai",
      message: "OpenAIProvider: the response was truncated (finish_reason length); raise the output-token limit",
    });
    reportOpenAIUsage(response, params, model);
    const content = response.choices?.[0]?.message?.content;
    if (content === null || content === undefined) {
      throw new Error("OpenAIProvider: the response contained no message content");
    }
    try {
      return JSON.parse(content) as unknown;
    } catch (error) {
      throw new Error(
        `OpenAIProvider: the response content was not valid JSON: ${(error as Error).message}`,
      );
    }
  }

  /**
   * Logical marker only (Python parity): OpenAI prefix-caches automatically with
   * no cache object and no warm-up call, so this makes no API request. The cache
   * "hit" surfaces as `prompt_tokens_details.cached_tokens` once the prefix is
   * re-sent. `created_at` is left for the prep activity to stamp (replay-safe);
   * `ttl_seconds` stays null — retention is provider-managed and no TTL is sent,
   * so none is recorded. `params.providerParams` is identity-only in TS (no
   * ProviderParams merge, #495): the handle records `params.model ?? default`,
   * exactly the model the fan-out calls will use.
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

  /** No-op: OpenAI's implicit prefix cache has no object to delete. */
  releaseCachedSession(): void {}
}

/**
 * Report token usage through the per-call sink (#478; Python `_openai_usage`).
 * The implicit prefix-cache hit lives under `prompt_tokens_details.cached_tokens`
 * (there is no write-side signal). Skipped entirely when the response carries no
 * usage fields, matching Python's None return.
 */
function reportOpenAIUsage(
  response: ChatCompletionResponse,
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
  const cachedTokens = usage.prompt_tokens_details?.cached_tokens;
  if (
    usage.prompt_tokens === undefined &&
    usage.completion_tokens === undefined &&
    usage.total_tokens === undefined &&
    cachedTokens === undefined
  ) {
    return;
  }
  const report: ProviderUsage = { model };
  if (usage.prompt_tokens !== undefined) {
    report.inputTokens = usage.prompt_tokens;
  }
  if (usage.completion_tokens !== undefined) {
    report.outputTokens = usage.completion_tokens;
  }
  if (usage.total_tokens !== undefined) {
    report.totalTokens = usage.total_tokens;
  }
  if (cachedTokens !== undefined) {
    report.cacheReadTokens = cachedTokens;
  }
  params.usageSink(report);
}

/**
 * Map typed content parts to OpenAI-native chat parts (Python `_to_openai_content_parts`): text
 * passes through; artifact / artifact_group parts resolve via the supplied groups (their optional
 * `text` becomes a text part first); an `openai` provider_extension contributes its payload
 * verbatim; anything else — including an extension for another provider — fails loud and typed.
 */
async function contentToOpenAIParts(
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
        for (const artifact of artifactsForGroup(artifacts, part.group, "openai")) {
          payload.push(await toOpenAIArtifactPart(artifact));
        }
        break;
      }
      case "artifact": {
        if (part.text !== undefined) {
          payload.push({ type: "text", text: part.text });
        }
        payload.push(await toOpenAIArtifactPart(artifactForName(artifacts, part.artifact, "openai")));
        break;
      }
      case "provider_extension":
        if (part.provider !== "openai") {
          throw new ProviderConfigError(
            `content part provider extension ${JSON.stringify(part.provider)} is not for OpenAI`,
            { provider: "openai" },
          );
        }
        payload.push({ ...part.payload });
        break;
      default: {
        const unreachable: never = part;
        throw new ProviderConfigError(
          `unsupported OpenAI content part: ${JSON.stringify((unreachable as { type?: unknown }).type)}`,
          { provider: "openai" },
        );
      }
    }
  }
  return payload;
}

/**
 * One resolved artifact as an OpenAI-native part (Python `_to_openai_artifact_part`): images as
 * `image_url` (URL or base64 data URL), text-like local files inlined as text, local PDFs as
 * base64 `file` parts, OpenAI provider files by id — anything else cannot ride the
 * chat-completions path and fails loud.
 */
async function toOpenAIArtifactPart(artifact: ResolvedArtifact): Promise<Record<string, unknown>> {
  const source = artifact.ref.source;
  if (artifact.kind === "image") {
    if (source.type === "url") {
      return { type: "image_url", image_url: { url: source.url } };
    }
    if (source.type === "local_path" && artifact.local_path !== undefined) {
      // Python truthiness (`media_type or "image/png"`): an empty string counts as missing.
      const mediaType = artifact.media_type !== undefined && artifact.media_type !== "" ? artifact.media_type : "image/png";
      const encoded = (await readFile(artifact.local_path)).toString("base64");
      return { type: "image_url", image_url: { url: `data:${mediaType};base64,${encoded}` } };
    }
  }
  if (source.type === "local_path" && artifact.local_path !== undefined) {
    if (isTextLikeMediaType(artifact.media_type)) {
      return { type: "text", text: await readFile(artifact.local_path, "utf-8") };
    }
    if (artifact.media_type === "application/pdf") {
      const encoded = (await readFile(artifact.local_path)).toString("base64");
      return {
        type: "file",
        file: { filename: basename(artifact.local_path), file_data: `data:application/pdf;base64,${encoded}` },
      };
    }
  }
  if (source.type === "provider_file" && source.provider === "openai") {
    return { type: "file", file: { file_id: source.file_id } };
  }
  throw new ProviderConfigError(
    "OpenAIProvider cannot attach this artifact through the chat-completions path",
    { provider: "openai" },
  );
}

function isTextLikeMediaType(mediaType: string | undefined): boolean {
  return (
    mediaType !== undefined &&
    (mediaType.startsWith("text/") || ["application/json", "application/xml", "text/csv"].includes(mediaType))
  );
}
