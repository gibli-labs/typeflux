import { TypeSafeClient } from "@typesafe-ai/sdk";
import { TypeSafeProvider } from "@typeflux/temporal";
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
