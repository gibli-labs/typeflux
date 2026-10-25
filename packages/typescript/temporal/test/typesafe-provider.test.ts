import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import {
  defineActivity, executeActivity, ProviderAuthError, ProviderConfigError,
  ProviderRateLimitError, ProviderTransientError, TypeSafeProvider, type TypeSafeTransport,
} from "../src/index.js";
import { InMemoryCacheStore } from "../src/execute.js";

const questions = {
  route: { type: "choice", instructions: "Route this ticket", criteria: { billing: "Payments", technical: "Bugs" } },
  urgency: { type: "score", instructions: "Urgency", criteria: ["Routine", "Urgent"] },
  refund: { type: "noul", instructions: "Requests a refund" },
};
const messages = [{ role: "system", content: JSON.stringify(questions) }, { role: "user", content: "Refund my duplicate charge" }];
const response = () => ({ model: "jev-1.14.0", answers: {
  route: { type: "choice", choice: "billing", confidence: 0.9, probabilities: { billing: 0.95, technical: 0.05 } },
  urgency: { type: "score", score: 0.7, confidence: 0.8, probabilities: { "0": 0.3, "1": 0.7 }, legend: { "0": "Routine", "1": "Urgent" } },
  refund: { type: "noul", noul: 0.99 },
}, usage: { input_tokens: 100, output_tokens: 20 } });
const call = { messages, outputSchema: {} };
function fixture() {
  const systemOne = vi.fn<TypeSafeTransport["systemOne"]>().mockResolvedValue(response());
  const transport = { systemOne };
  return { transport, systemOne, provider: new TypeSafeProvider(transport, { model: "jev-latest" }) };
}
const output = z.object({
  route: z.object({ type: z.literal("choice"), choice: z.enum(["billing", "technical"]), confidence: z.number(), probabilities: z.object({ billing: z.number(), technical: z.number() }) }),
  urgency: z.object({ type: z.literal("score"), score: z.number(), confidence: z.number(), probabilities: z.object({ "0": z.number(), "1": z.number() }), legend: z.object({ "0": z.string(), "1": z.string() }) }),
  refund: z.object({ type: z.literal("noul"), noul: z.number().min(0).max(1) }),
});
const activity = defineActivity({ name: "decide", prompt: { name: "p" }, input: z.object({ text: z.string() }), output, cache: {} });

describe("TypeSafe Jev", () => {
  it("preserves native decisions and usage through activity validation", async () => {
    const { provider, systemOne } = fixture();
    const usageSink = vi.fn(), providerModelGuard = vi.fn();
    const result = await executeActivity(activity, { text: "ticket" }, { provider, messages, usageSink, providerModelGuard, providerParams: { timeout: 2 } });
    expect(result).toEqual(response().answers);
    expect(systemOne).toHaveBeenCalledExactlyOnceWith({ model: "jev-latest", questions, state: messages[1]!.content }, { timeoutMs: 2000 });
    expect(usageSink).toHaveBeenCalledWith({ model: "jev-latest", reportedModel: "jev-1.14.0", inputTokens: 100, outputTokens: 20 });
    expect(providerModelGuard.mock.calls[0]?.[0]).toMatchObject({ providerName: "typesafe", model: "jev-latest" });
    expect(provider.supportsSessionCache).toBe(false);
  });
  it("partitions caches by model and honors explicit overrides", async () => {
    const { provider, transport, systemOne } = fixture();
    const options = { provider, messages, cacheStore: new InMemoryCacheStore() };
    await executeActivity(activity, { text: "ticket" }, options);
    await executeActivity(activity, { text: "ticket" }, options);
    expect(systemOne).toHaveBeenCalledOnce();
    await executeActivity(activity, { text: "ticket" }, { ...options, provider: new TypeSafeProvider(transport) });
    expect(systemOne).toHaveBeenCalledTimes(2);
    await provider.structuredCall({ ...call, model: "jev-1.13.0" });
    expect(systemOne.mock.calls.at(-1)?.[0].model).toBe("jev-1.13.0");
  });
  it.each([[401, ProviderAuthError], [403, ProviderAuthError], [429, ProviderRateLimitError], [529, ProviderTransientError], [503, ProviderTransientError], [422, ProviderConfigError]])("classifies status %s without transport secrets", async (status, errorType) => {
    const { provider, systemOne } = fixture();
    systemOne.mockRejectedValue(Object.assign(new Error("fixture-secret"), { status, headers: { "retry-after": "2" } }));
    const error = await provider.structuredCall(call).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(errorType);
    expect(String(error)).not.toContain("fixture-secret");
    expect(error).not.toHaveProperty("cause");
    expect(systemOne).toHaveBeenCalledOnce();
    if ([429, 529, 503].includes(status as number)) expect(error).toHaveProperty("retryAfterSeconds", 2);
  });
  it("retries overload through the executor and propagates cancellation", async () => {
    const { provider, systemOne } = fixture();
    systemOne.mockRejectedValueOnce(Object.assign(new Error("overloaded"), { status: 529 }));
    await executeActivity(activity, { text: "ticket" }, { provider, messages, transientRetries: 1 });
    expect(systemOne).toHaveBeenCalledTimes(2);
    const controller = new AbortController();
    systemOne.mockImplementation(async (_request, options) => {
      expect(options?.signal).toBe(controller.signal);
      controller.abort(new Error("cancelled"));
      throw new Error("raw transport error");
    });
    await expect(provider.structuredCall({ ...call, signal: controller.signal })).rejects.toThrow("cancelled");
  });
  it.each([{}, { q: { type: "box", instructions: "Find it" } }, { q: { type: "score", instructions: "Rate", criteria: ["Only one"] } }])("rejects unsupported questions before transport: %j", async (bad) => {
    const { provider, systemOne } = fixture();
    await expect(provider.structuredCall({ ...call, messages: [{ role: "system", content: JSON.stringify(bad) }, messages[1]!] })).rejects.toBeInstanceOf(ProviderConfigError);
    expect(systemOne).not.toHaveBeenCalled();
  });
  it("rejects sampling params and malformed answers", async () => {
    const { provider, systemOne } = fixture();
    await expect(provider.structuredCall({ ...call, providerParams: { temperature: 0 } })).rejects.toThrow("temperature");
    expect(systemOne).not.toHaveBeenCalled();
    const malformed = response(); malformed.answers.refund.noul = 1.1;
    systemOne.mockResolvedValue(malformed);
    await expect(provider.structuredCall(call)).rejects.toThrow("noul probability");
    systemOne.mockResolvedValue({ answers: {} });
    await expect(provider.structuredCall(call)).rejects.toThrow("do not match");
  });
  it("preserves roles in additional context including validation repair", async () => {
    const { provider, systemOne } = fixture();
    const repair = { role: "system", content: "Use the configured output contract" };
    await provider.structuredCall({ ...call, messages: [...messages, repair] });
    expect(systemOne.mock.calls[0]?.[0].state).toEqual([messages[1], repair]);
  });
});
