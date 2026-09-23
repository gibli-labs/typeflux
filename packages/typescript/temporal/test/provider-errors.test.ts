import { describe, expect, it } from "vitest";

import type {
  AnthropicMessagesTransport,
  GeminiGenerateContentTransport,
  JsonSchema,
  OpenAIChatTransport,
} from "../src/index.js";
import {
  AnthropicProvider,
  GeminiProvider,
  OpenAIProvider,
  ProviderConfigError,
  ProviderRateLimitError,
  ProviderTransientError,
} from "../src/index.js";

const outputSchema: JsonSchema = {
  type: "object",
  properties: { summary: { type: "string" } },
  required: ["summary"],
  additionalProperties: false,
};

const messages = [{ role: "user", content: "go" }];

/** A transport error the way provider SDKs surface it: an Error with a `status`. */
const transportError = (status: number, message = "transport failure"): Error =>
  Object.assign(new Error(message), { status });

interface ProviderErrorCase {
  provider: string;
  /** Realistic retryable statuses for this provider's API. */
  retryableStatuses: number[];
  /** Runs a structuredCall against a transport that rejects with `error`. */
  callWithTransportError: (error: unknown) => Promise<unknown>;
  /** Runs a structuredCall whose response is truncated at the token limit. */
  truncatedCall: () => Promise<unknown>;
}

const cases: ProviderErrorCase[] = [
  {
    provider: "AnthropicProvider",
    // 529 is Anthropic's "overloaded" status; 408/409/429/5xx are shared transport codes.
    retryableStatuses: [408, 409, 429, 500, 529],
    callWithTransportError: (error) => {
      const transport: AnthropicMessagesTransport = {
        messages: { create: () => Promise.reject(error) },
      };
      return new AnthropicProvider(transport, { model: "claude-sonnet-4-6" }).structuredCall({
        messages,
        outputSchema,
      });
    },
    truncatedCall: () => {
      const transport: AnthropicMessagesTransport = {
        messages: {
          create: () =>
            Promise.resolve({
              content: [{ type: "text", text: JSON.stringify({ summary: "partial" }) }],
              stop_reason: "max_tokens",
            }),
        },
      };
      return new AnthropicProvider(transport, { model: "claude-sonnet-4-6" }).structuredCall({
        messages,
        outputSchema,
      });
    },
  },
  {
    provider: "OpenAIProvider",
    retryableStatuses: [408, 409, 429, 500, 503],
    callWithTransportError: (error) => {
      const transport: OpenAIChatTransport = {
        chat: { completions: { create: () => Promise.reject(error) } },
      };
      return new OpenAIProvider(transport, { model: "gpt-test" }).structuredCall({
        messages,
        outputSchema,
      });
    },
    truncatedCall: () => {
      const transport: OpenAIChatTransport = {
        chat: {
          completions: {
            create: () =>
              Promise.resolve({
                choices: [
                  {
                    finish_reason: "length",
                    message: { content: JSON.stringify({ summary: "partial" }) },
                  },
                ],
              }),
          },
        },
      };
      return new OpenAIProvider(transport, { model: "gpt-test" }).structuredCall({
        messages,
        outputSchema,
      });
    },
  },
  {
    provider: "GeminiProvider",
    retryableStatuses: [408, 409, 429, 500, 503],
    callWithTransportError: (error) => {
      const transport: GeminiGenerateContentTransport = {
        generateContent: () => Promise.reject(error),
      };
      return new GeminiProvider(transport, { model: "gemini-2.5-flash" }).structuredCall({
        messages,
        outputSchema,
      });
    },
    truncatedCall: () => {
      const transport: GeminiGenerateContentTransport = {
        generateContent: () =>
          Promise.resolve({
            candidates: [
              {
                content: { parts: [{ text: JSON.stringify({ summary: "partial" }) }] },
                finishReason: "MAX_TOKENS",
              },
            ],
          }),
      };
      return new GeminiProvider(transport, { model: "gemini-2.5-flash" }).structuredCall({
        messages,
        outputSchema,
      });
    },
  },
];

describe.each(cases)("$provider error classification", (c) => {
  it.each(c.retryableStatuses)(
    "wraps a retryable transport error (status %d) as ProviderTransientError",
    async (status) => {
      await expect(c.callWithTransportError(transportError(status))).rejects.toBeInstanceOf(
        ProviderTransientError,
      );
    },
  );

  it("wraps a plain-object retryable rejection (SDKs may reject with non-Errors)", async () => {
    await expect(c.callWithTransportError({ status: 503 })).rejects.toBeInstanceOf(
      ProviderTransientError,
    );
  });

  it.each([400, 404])(
    "types a provider-side %d rejection as ProviderConfigError (cause preserved)",
    async (status) => {
      const error = transportError(status, "rejected by provider");
      const call = c.callWithTransportError(error);
      await expect(call).rejects.toBeInstanceOf(ProviderConfigError);
      await expect(call).rejects.not.toBeInstanceOf(ProviderTransientError);
      await expect(call).rejects.toMatchObject({ cause: error });
    },
  );

  it("types a plain-object 400 rejection as ProviderConfigError", async () => {
    await expect(c.callWithTransportError({ status: 400 })).rejects.toBeInstanceOf(
      ProviderConfigError,
    );
  });

  it("rethrows an auth failure (401) untyped until an auth error class exists", async () => {
    const error = transportError(401, "unauthorized");
    const call = c.callWithTransportError(error);
    await expect(call).rejects.toBe(error); // the exact error object, not a copy
    await expect(call).rejects.not.toBeInstanceOf(ProviderConfigError);
  });

  it("rethrows a status-less transport error as-is", async () => {
    const error = new Error("socket hang up");
    await expect(c.callWithTransportError(error)).rejects.toBe(error);
  });

  // #529: a 429 gets the DISTINCT rate-limit class (a ProviderTransientError
  // subclass, so the "429 is transient" assertions above keep holding).
  it("classifies a 429 as ProviderRateLimitError carrying the parsed Retry-After hint", async () => {
    const error = Object.assign(new Error("rate limited"), {
      status: 429,
      response: { headers: { "retry-after": "7" } },
    });
    const call = c.callWithTransportError(error);
    await expect(call).rejects.toBeInstanceOf(ProviderRateLimitError);
    await expect(call).rejects.toBeInstanceOf(ProviderTransientError);
    await expect(call).rejects.toMatchObject({ retryAfterSeconds: 7, cause: error });
  });

  it("classifies an SDK error named RateLimitError (no status) as ProviderRateLimitError", async () => {
    const error = Object.assign(new Error("rate limited"), { name: "RateLimitError" });
    await expect(c.callWithTransportError(error)).rejects.toBeInstanceOf(ProviderRateLimitError);
  });

  it("a 429 without a hint carries retryAfterSeconds undefined", async () => {
    const call = c.callWithTransportError(transportError(429));
    await expect(call).rejects.toBeInstanceOf(ProviderRateLimitError);
    await expect(call).rejects.toHaveProperty("retryAfterSeconds", undefined);
  });

  it("a NON-429 transient carries a Retry-After hint too (Python attaches it at every wrap site)", async () => {
    const error = Object.assign(new Error("overloaded"), {
      status: 503,
      headers: { "retry-after": "30" },
    });
    const call = c.callWithTransportError(error);
    await expect(call).rejects.toBeInstanceOf(ProviderTransientError);
    await expect(call).rejects.not.toBeInstanceOf(ProviderRateLimitError);
    await expect(call).rejects.toMatchObject({ retryAfterSeconds: 30 });
  });
});

describe.each(cases)("$provider truncation detection", (c) => {
  it("throws on a response truncated at the token limit", async () => {
    await expect(c.truncatedCall()).rejects.toThrow(/truncated/);
  });

  // #784: truncation is deterministic — the same token limit truncates the identical
  // call on every retry — so it must be ProviderConfigError, which the worker adapter
  // classifies as a non-retryable ApplicationFailure (parity with Python's
  // raise_if_truncated raising ProviderConfigError(retryable=False)).
  it("classifies truncation as ProviderConfigError so the worker fails it on attempt 1", async () => {
    await expect(c.truncatedCall()).rejects.toBeInstanceOf(ProviderConfigError);
  });
});
