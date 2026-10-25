import { TypeSafeClient } from "@typesafe-ai/sdk";
import { ProviderAuthError, ProviderConfigError, ProviderRateLimitError, ProviderTransientError, TypeSafeProvider } from "@typeflux/temporal";
import { expect, it, vi } from "vitest";

it("serializes native decisions with the official SDK and preserves timeout and cancellation options", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(new Response(JSON.stringify({
    model: "jev-1.14.0", answers: { refund: { type: "noul", noul: 0.99 } }, usage: { input_tokens: 20, output_tokens: 2 },
  }), { headers: { "content-type": "application/json" } }));
  const client = new TypeSafeClient({ apiKey: "fixture-key", fetch, retry: { maxRetries: 0 }, compression: false, logLevel: "off" });
  const provider = new TypeSafeProvider({ systemOne: (request, options) => client.systemOne(request, {
    ...(options?.signal ? { signal: options.signal } : {}),
    ...(options?.timeoutMs ? { timeout: options.timeoutMs } : {}),
  }) });
  const signal = new AbortController().signal;
  const questions = { refund: { type: "noul", instructions: "Requests a refund" } };
  expect(await provider.structuredCall({ messages: [{ role: "system", content: JSON.stringify(questions) }, { role: "user", content: "Refund please" }], outputSchema: {}, signal, providerParams: { timeout: 2 } })).toEqual({ refund: { type: "noul", noul: 0.99 } });
  expect(fetch).toHaveBeenCalledOnce();
  const [url, init] = fetch.mock.calls[0]!;
  expect(String(url)).toBe("https://api.typesafe.ai/v1/systemone");
  expect(JSON.parse(init!.body as string)).toEqual({ model: "jev-1.14.0", questions, state: "Refund please" });
  expect(new Headers(init?.headers).get("authorization")).toBe("Bearer fixture-key");
  expect(init?.signal).toBeInstanceOf(AbortSignal);
});

it("classifies official SDK HTTP errors and retry hints", async () => {
  const cases = [
    [401, {}, ProviderAuthError, undefined],
    [403, {}, ProviderAuthError, undefined],
    [408, {}, ProviderTransientError, undefined],
    [409, {}, ProviderTransientError, undefined],
    [429, { "retry-after": "2" }, ProviderRateLimitError, 2],
    [429, { "retry-after-ms": "1250", "retry-after": "9" }, ProviderRateLimitError, 1.25],
    [503, { "retry-after": "2" }, ProviderTransientError, 2],
    [529, { "retry-after-ms": "1250" }, ProviderTransientError, 1.25],
    [422, {}, ProviderConfigError, undefined],
  ] as const;
  for (const [status, headers, errorType, delay] of cases) {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async () => new Response(
      JSON.stringify({ message: "fixture-secret" }), { status, headers: { "content-type": "application/json", ...headers } },
    ));
    const client = new TypeSafeClient({ apiKey: "fixture-key", fetch, retry: { maxRetries: 0 }, compression: false, logLevel: "off" });
    const provider = new TypeSafeProvider({ systemOne: (request) => client.systemOne(request) });
    const messages = [
      { role: "system", content: JSON.stringify({ refund: { type: "noul", instructions: "Requests a refund" } }) },
      { role: "user", content: "Refund please" },
    ];
    const error: unknown = await provider.structuredCall({ messages, outputSchema: {} }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(errorType);
    expect(String(error)).not.toContain("fixture-secret");
    expect(error).not.toHaveProperty("cause");
    expect(fetch).toHaveBeenCalledOnce();
    if (error instanceof ProviderTransientError) expect(error.retryAfterSeconds).toBe(delay);
  }
});
