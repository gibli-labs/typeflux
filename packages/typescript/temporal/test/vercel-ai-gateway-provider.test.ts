import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import {
  ActivityValidationError, CollectingObserver, defineActivity, executeActivity, ProviderAuthError,
  ProviderConfigError, ProviderRateLimitError, ProviderTransientError, VercelAIGatewayProvider,
  type ChatCompletionResponse, type OpenAIChatTransport, type ProviderUsage,
} from "../src/index.js";
import { InMemoryCacheStore } from "../src/execute.js";

const model = "openai/gpt-4o-mini";
const messages = [{ role: "user", content: "Return label ok" }];
const outputSchema = { type: "object", properties: { label: { type: "string" } } };
const call = { messages, outputSchema };
const response = (): ChatCompletionResponse => ({
  model: "gpt-4o-mini-2024-07-18",
  choices: [{ message: { content: '{"label":"ok"}' }, finish_reason: "stop" }],
  usage: { prompt_tokens: 5, completion_tokens: 4, total_tokens: 9, prompt_tokens_details: { cached_tokens: 2 } },
  provider_metadata: { gateway: { routing: { finalProvider: "azure" } } },
});
function fixture() {
  const create = vi.fn<OpenAIChatTransport["chat"]["completions"]["create"]>().mockResolvedValue(response());
  const transport = { chat: { completions: { create } } };
  return { create, transport, provider: new VercelAIGatewayProvider(transport, { model }) };
}
const activity = defineActivity({
  name: "classify", prompt: { name: "p" }, input: z.object({ text: z.string() }),
  output: z.object({ label: z.string() }), validationRetries: 1, cache: {},
});

describe("Vercel AI Gateway", () => {
  it("executes a validated activity and reports separate gateway model and upstream identity", async () => {
    const { provider, create } = fixture();
    const usage: ProviderUsage[] = [];
    const guard = vi.fn();
    const observer = new CollectingObserver();
    const result = await executeActivity(activity, { text: "hi" }, {
      provider, messages, observer, providerModelGuard: guard, usageSink: (u) => usage.push(u),
      providerParams: { temperature: 0, max_tokens: 64, top_p: 0.9, stop: "END", timeout: 30 },
    });
    expect(result).toEqual({ label: "ok" });
    expect(guard.mock.calls[0]?.[0]).toMatchObject({ providerName: "vercel-ai-gateway", model });
    expect(create).toHaveBeenCalledOnce();
    expect(create.mock.calls[0]?.[0]).toMatchObject({ model, temperature: 0, max_tokens: 64, top_p: 0.9, stop: ["END"], response_format: { type: "json_schema" } });
    expect(create.mock.calls[0]?.[1]).toEqual({ timeoutMs: 30_000 });
    expect(usage).toEqual([{ model, reportedModel: "gpt-4o-mini-2024-07-18", upstreamProvider: "azure", inputTokens: 5, outputTokens: 4, totalTokens: 9, cacheReadTokens: 2 }]);
    expect(provider.supportsSessionCache).toBe(false);
    expect(observer.activities[0]?.model).toBe(model);
    expect(observer.activities[0]?.generations[0]?.metadata).toMatchObject({ usage: usage[0] });
  });

  it("partitions cache and enforces policy using the constructor model before a cache hit", async () => {
    const { provider, create, transport } = fixture();
    const cacheStore = new InMemoryCacheStore();
    const options = { provider, messages, cacheStore };
    await executeActivity(activity, { text: "hi" }, options);
    await executeActivity(activity, { text: "hi" }, options);
    expect(create).toHaveBeenCalledOnce();
    const other = new VercelAIGatewayProvider(transport, { model: "anthropic/claude-haiku-4.5" });
    await executeActivity(activity, { text: "hi" }, { ...options, provider: other });
    expect(create).toHaveBeenCalledTimes(2);
    await expect(executeActivity(activity, { text: "hi" }, { ...options,
      providerModelGuard: () => { throw new Error("policy denied"); },
    })).rejects.toThrow("policy denied");
    expect(create).toHaveBeenCalledTimes(2);
  });

  it("repairs invalid output through the existing activity validation loop", async () => {
    const { provider, create } = fixture();
    create.mockResolvedValueOnce({ choices: [{ message: { content: '{"label":12}' } }] });
    expect(await executeActivity(activity, { text: "hi" }, { provider, messages })).toEqual({ label: "ok" });
    expect(create).toHaveBeenCalledTimes(2);
    create.mockResolvedValue({ choices: [{ message: { content: '{"label":12}' } }] });
    await expect(executeActivity(activity, { text: "hi" }, { provider, messages })).rejects.toBeInstanceOf(ActivityValidationError);
  });

  it.each(["length", "content_filter"])("rejects %s without usage", async (finish_reason) => {
    const { provider, create } = fixture();
    create.mockResolvedValue({ ...response(), choices: [{ message: { content: '{"label":"ok"}' }, finish_reason }] });
    const usageSink = vi.fn();
    await expect(provider.structuredCall({ ...call, usageSink })).rejects.toBeInstanceOf(ProviderConfigError);
    expect(usageSink).not.toHaveBeenCalled();
  });

  it("rejects invalid JSON without including response content", async () => {
    const { provider, create } = fixture();
    create.mockResolvedValue({ choices: [{ message: { content: "fixture-secret {" } }] });
    await expect(provider.structuredCall(call)).rejects.toThrow("returned invalid JSON");
  });

  it.each([
    [401, ProviderAuthError], [403, ProviderAuthError], [429, ProviderRateLimitError],
    [408, ProviderTransientError], [409, ProviderTransientError], [503, ProviderTransientError],
    [400, ProviderConfigError], [422, ProviderConfigError],
  ])("classifies status %s without leaking transport secrets", async (status, errorType) => {
    const { provider, create } = fixture();
    create.mockRejectedValue(Object.assign(new Error("fixture-secret"), { status, headers: { "retry-after": "2" } }));
    const error = await provider.structuredCall(call).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(errorType);
    expect(String(error)).not.toContain("fixture-secret");
    expect(error).not.toHaveProperty("cause");
    if (status === 429 || status === 503) expect(error).toHaveProperty("retryAfterSeconds", 2);
    expect(create).toHaveBeenCalledOnce();
  });

  it("retries transient failures through the executor and propagates cancellation", async () => {
    const { provider, create } = fixture();
    create.mockRejectedValueOnce({ status: 503 });
    expect(await executeActivity(activity, { text: "hi" }, { provider, messages, transientRetries: 1 })).toEqual({ label: "ok" });
    expect(create).toHaveBeenCalledTimes(2);
    const controller = new AbortController();
    create.mockImplementation(async (_request, options) => {
      expect(options?.signal).toBe(controller.signal);
      controller.abort();
      throw new Error("raw SDK error");
    });
    await expect(provider.structuredCall({ ...call, signal: controller.signal })).rejects.toHaveProperty("name", "AbortError");
  });

  it.each([{ seed: 1 }, { thinking_budget: 1 }, { providerOptions: {} }, { max_tokens: -1 }, { top_p: NaN }, { timeout: 0 }])(
    "rejects unsupported or invalid parameters before calling the transport: %j", async (providerParams) => {
      const { provider, create } = fixture();
      await expect(provider.structuredCall({ ...call, providerParams })).rejects.toBeInstanceOf(ProviderConfigError);
      expect(create).not.toHaveBeenCalled();
    },
  );

  it("requires an explicit namespaced model and text input", async () => {
    const { transport, provider, create } = fixture();
    expect(() => new VercelAIGatewayProvider(transport, { model: "gpt-4o-mini" })).toThrow("author/model");
    await expect(provider.structuredCall({ ...call, messages: [{ role: "user", content: [{ type: "provider_extension", provider: "openai", payload: {} }] }] })).rejects.toThrow("text");
    expect(create).not.toHaveBeenCalled();
  });
});
