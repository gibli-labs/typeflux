/** Text-only structured output through Vercel's Chat Completions API.
 * The transport owns authentication; the YAML runtime can build the optional
 * OpenAI SDK adapter. No upstream model capability or cache support is assumed.
 */
import {
  callOptions,
  mergeProviderParams,
  ProviderAuthError,
  ProviderConfigError,
  ProviderRateLimitError,
  ProviderTransientError,
  raiseIfTruncated,
  retryAfterSecondsFrom,
  type ModelProvider,
  type ProviderUsage,
  type StructuredCallParams,
} from "./execute.js";
import {
  reportOpenAIUsage,
  type ChatCompletionRequest,
  type OpenAIChatTransport,
} from "./openai-provider.js";

export const VERCEL_AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1";
export const VERCEL_AI_GATEWAY_PARAMS: ReadonlySet<string> = new Set([
  "model", "temperature", "max_tokens", "top_p", "stop", "timeout",
]);
const PROVIDER = "vercel-ai-gateway";

export function validateGatewayModel(model: unknown): asserts model is string {
  if (typeof model !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_./:-]+$/.test(model)) {
    throw config("requires an explicit author/model ID");
  }
}

function config(message: string): ProviderConfigError {
  return new ProviderConfigError(`Vercel AI Gateway ${message}`, { provider: PROVIDER });
}

export class VercelAIGatewayProvider implements ModelProvider {
  readonly providerName = PROVIDER;
  readonly supportsSessionCache = false;
  readonly defaultProviderParams: Readonly<Record<string, unknown>>;

  constructor(private readonly transport: OpenAIChatTransport, options: { model: string }) {
    validateGatewayModel(options.model);
    this.defaultProviderParams = Object.freeze({ model: options.model });
  }

  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    const behavior = mergeProviderParams(this.defaultProviderParams, params.providerParams);
    const model = params.model ?? behavior["model"];
    validateGatewayModel(model);
    for (const [key, value] of Object.entries(behavior)) {
      if (value === undefined || value === null) continue;
      if (!VERCEL_AI_GATEWAY_PARAMS.has(key)) throw config(`does not support provider param ${key}`);
      if (key === "model") validateGatewayModel(value);
      else if (key === "stop") {
        if (!(typeof value === "string" && value.length > 0) &&
            !(Array.isArray(value) && value.length > 0 && value.every((v) => typeof v === "string" && v.length > 0))) {
          throw config("requires stop to be a nonempty string or string array");
        }
      } else if (typeof value !== "number" || !Number.isFinite(value) ||
          (key === "temperature" && (value < 0 || value > 2)) ||
          (key === "top_p" && (value < 0 || value > 1)) ||
          (key === "timeout" && value <= 0) ||
          (key === "max_tokens" && (!Number.isInteger(value) || value <= 0))) {
        throw config(`received invalid ${key}`);
      }
    }
    if ((params.artifacts?.length ?? 0) > 0 || params.cachedSession?.supported === true) {
      throw config("does not support artifacts or explicit session caches");
    }
    const messages = params.messages.map((message) => {
      if (!["system", "user", "assistant"].includes(message.role)) {
        throw config("supports only system/user/assistant text messages");
      }
      const content = typeof message.content === "string" ? message.content : message.content.map((part) => {
        if (part.type !== "text") throw config("supports only text content parts");
        return { type: "text", text: part.text };
      });
      return { role: message.role, content, ...(message.name !== undefined ? { name: message.name } : {}) };
    });
    const request: ChatCompletionRequest = {
      model, messages,
      response_format: {
        type: "json_schema",
        json_schema: { name: "response", schema: params.outputSchema, strict: true },
      },
    };
    for (const key of ["temperature", "max_tokens", "top_p"] as const) {
      const value = behavior[key];
      if (typeof value === "number") request[key] = value;
    }
    const stop = behavior["stop"];
    if (typeof stop === "string") request.stop = [stop];
    else if (Array.isArray(stop)) request.stop = stop as string[];
    params.signal?.throwIfAborted();
    let response;
    try {
      response = await this.transport.chat.completions.create(request, callOptions(params));
    } catch (error) {
      params.signal?.throwIfAborted();
      throw gatewayError(error);
    }
    params.signal?.throwIfAborted();
    const choice = response.choices?.[0];
    raiseIfTruncated(choice?.finish_reason === "length", {
      provider: PROVIDER, message: "Vercel AI Gateway response was truncated; raise max_tokens",
    });
    if (choice?.message.refusal || choice?.finish_reason === "content_filter") {
      throw config("refused the structured request");
    }
    const content = choice?.message.content;
    if (typeof content !== "string") throw config("returned no structured output");
    let result: unknown;
    try {
      result = JSON.parse(content);
    } catch {
      // Never copy response content into an error or observation.
      throw config("returned invalid JSON");
    }
    if (params.usageSink !== undefined) {
      let usage: ProviderUsage = { model };
      reportOpenAIUsage(response, { ...params, usageSink: (value) => { usage = value; } }, model);
      const reportedModel = identity(response.model);
      const upstreamProvider = identity(response.provider_metadata?.gateway?.routing?.finalProvider);
      params.usageSink({
        ...usage,
        ...(reportedModel !== undefined ? { reportedModel } : {}),
        ...(upstreamProvider !== undefined ? { upstreamProvider } : {}),
      });
    }
    // The executor performs Zod validation and existing validation-repair retries.
    return result;
  }
}

function identity(value: unknown): string | undefined {
  return typeof value === "string" && /^[A-Za-z0-9_./:-]{1,200}$/.test(value) ? value : undefined;
}

function gatewayError(error: unknown): Error {
  const record = error as { status?: unknown; statusCode?: unknown; name?: string; code?: string } | null;
  const status = record?.status ?? record?.statusCode;
  // Raw SDK errors can include credentials/response bodies. Preserve only the
  // retry class and Retry-After hint, never the cause or raw error text.
  const hint = retryAfterSecondsFrom(error);
  const options = hint === undefined ? undefined : { retryAfterSeconds: hint };
  if (status === 401 || status === 403) {
    return new ProviderAuthError("Vercel AI Gateway authentication failed", { provider: PROVIDER });
  }
  if (status === 429 || record?.name === "RateLimitError") {
    return new ProviderRateLimitError("Vercel AI Gateway rate limit", options);
  }
  if (status === 408 || status === 409 || (typeof status === "number" && status >= 500) ||
      ["APIConnectionError", "APITimeoutError", "TimeoutError"].includes(record?.name ?? "") ||
      ["ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN"].includes(record?.code ?? "")) {
    return new ProviderTransientError("Vercel AI Gateway transient transport failure", options);
  }
  return config("rejected the structured request");
}
