import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ChatCompletionRequest, OpenAIChatTransport } from "../src/index.js";
import { defineActivity, executeActivity, OpenAIProvider } from "../src/index.js";

/** A fake OpenAI-style transport that records requests and returns queued contents. */
function fakeTransport(contents: (string | null)[]): {
  transport: OpenAIChatTransport;
  requests: ChatCompletionRequest[];
} {
  const requests: ChatCompletionRequest[] = [];
  const transport: OpenAIChatTransport = {
    chat: {
      completions: {
        create: (request) => {
          requests.push(request);
          const content = contents.length > 0 ? contents.shift()! : null;
          return Promise.resolve({ choices: [{ message: { content } }] });
        },
      },
    },
  };
  return { transport, requests };
}

const schema = { type: "object", properties: { label: { type: "string" } } };
const messages = [{ role: "user", content: "Classify: hi", name: "alice" }];

describe("OpenAIProvider (#430)", () => {
  it("builds a strict json_schema request and returns the parsed content", async () => {
    const { transport, requests } = fakeTransport([JSON.stringify({ label: "billing" })]);
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });

    const result = await provider.structuredCall({ messages, outputSchema: schema });

    expect(result).toEqual({ label: "billing" });
    expect(requests).toHaveLength(1);
    const request = requests[0]!;
    expect(request.model).toBe("gpt-test");
    expect(request.messages).toEqual([{ role: "user", content: "Classify: hi", name: "alice" }]);
    expect(request.response_format).toEqual({
      type: "json_schema",
      json_schema: { name: "response", schema, strict: true },
    });
  });

  it("prefers the call model over the default and honors schemaName", async () => {
    const { transport, requests } = fakeTransport([JSON.stringify({ label: "x" })]);
    const provider = new OpenAIProvider(transport, { model: "default-model", schemaName: "verdict" });

    await provider.structuredCall({ messages, outputSchema: schema, model: "call-model" });

    expect(requests[0]!.model).toBe("call-model");
    expect(requests[0]!.response_format.json_schema.name).toBe("verdict");
  });

  it("throws when no model is resolvable", async () => {
    const { transport } = fakeTransport([JSON.stringify({ label: "x" })]);
    const provider = new OpenAIProvider(transport);
    await expect(provider.structuredCall({ messages, outputSchema: schema })).rejects.toThrow(
      /model is required/,
    );
  });

  it("throws when the response has no content", async () => {
    const { transport } = fakeTransport([null]);
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    await expect(provider.structuredCall({ messages, outputSchema: schema })).rejects.toThrow(
      /no message content/,
    );
  });

  it("throws a clear error (not a TypeError) when choices is empty", async () => {
    const transport: OpenAIChatTransport = {
      chat: { completions: { create: () => Promise.resolve({ choices: [] }) } },
    };
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    await expect(provider.structuredCall({ messages, outputSchema: schema })).rejects.toThrow(
      /no message content/,
    );
  });

  it("honors strict: false for non-strict-capable endpoints", async () => {
    const { transport, requests } = fakeTransport([JSON.stringify({ label: "x" })]);
    const provider = new OpenAIProvider(transport, { model: "gpt-test", strict: false });
    await provider.structuredCall({ messages, outputSchema: schema });
    expect(requests[0]!.response_format.json_schema.strict).toBe(false);
  });

  it("throws a clear error when the content is not valid JSON", async () => {
    const { transport } = fakeTransport(["not json {"]);
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    await expect(provider.structuredCall({ messages, outputSchema: schema })).rejects.toThrow(
      /not valid JSON/,
    );
  });

  // Transport error classification (transient/config/auth) is covered for all
  // providers in provider-errors.test.ts.

  it("drives executeActivity end-to-end to a validated typed output", async () => {
    const Input = z.object({ text: z.string() });
    const Output = z.object({ label: z.enum(["billing", "support"]), score: z.number() });
    const activity = defineActivity({
      name: "classify",
      prompt: { name: "p/classify", label: "production" },
      input: Input,
      output: Output,
    });
    const { transport, requests } = fakeTransport([JSON.stringify({ label: "support", score: 0.9 })]);
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });

    const out = await executeActivity(activity, { text: "hi" }, {
      provider,
      messages: [{ role: "user", content: "Classify: hi" }],
    });

    expect(out).toEqual({ label: "support", score: 0.9 });
    // The provider received the activity's provider-safe output schema.
    expect(requests[0]!.response_format.json_schema.schema).toEqual(activity.outputProviderSchema);
  });
});

describe("OpenAIProvider session cache (#478 PR4)", () => {
  /** A transport variant whose response carries usage fields. */
  function usageTransport(usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    prompt_tokens_details?: { cached_tokens?: number };
  }): { transport: OpenAIChatTransport; requests: ChatCompletionRequest[]; calls: () => number } {
    const requests: ChatCompletionRequest[] = [];
    const transport: OpenAIChatTransport = {
      chat: {
        completions: {
          create: (request) => {
            requests.push(request);
            return Promise.resolve({
              choices: [{ message: { content: JSON.stringify({ label: "ok" }) } }],
              ...(usage !== undefined ? { usage } : {}),
            });
          },
        },
      },
    };
    return { transport, requests, calls: () => requests.length };
  }

  it("declares the prefix capability and prepares WITHOUT touching the transport", () => {
    const { transport, calls } = usageTransport();
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    expect(provider.supportsSessionCache).toBe(true);
    expect(provider.sessionCacheStyle).toBe("prefix");
    const handle = provider.prepareCachedSession({
      messages: [{ role: "system", content: "stable" }],
      artifacts: [],
      identityHash: "i".repeat(64),
      ttlSeconds: 600,
    });
    expect(calls()).toBe(0);
    expect(handle).toEqual({
      provider: "openai",
      identity_hash: "i".repeat(64),
      supported: true,
      style: "prefix",
      cache_id: null,
      model: "gpt-test",
      created_at: null,
      // Implicit cache, provider-managed retention — no TTL is sent or recorded.
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    });
    expect(() => provider.releaseCachedSession()).not.toThrow();
  });

  it("an engaged handle changes NOTHING about the request (implicit caching)", async () => {
    const { transport, requests } = usageTransport();
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    const cachedSession = {
      provider: "openai",
      identity_hash: "i".repeat(64),
      supported: true,
      style: "prefix" as const,
      cache_id: null,
      model: null,
      created_at: null,
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    };
    await provider.structuredCall({ messages, outputSchema: schema, cachedSession });
    await provider.structuredCall({ messages, outputSchema: schema });
    expect(requests[0]).toEqual(requests[1]);
  });

  it("#362: preserves prefix composition order (reference turn ahead of the per-item query)", async () => {
    // OpenAI has no explicit breakpoint (implicit byte-prefix caching): the only
    // thing that matters is ORDER. The executor's prefix composition puts the stable
    // reference turn ahead of the varying query; the provider must serialize it as-is
    // so the reference can enter the implicit cached prefix.
    const { transport, requests } = fakeTransport([JSON.stringify({ label: "ok" })]);
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    const cachedSession = {
      provider: "openai",
      identity_hash: "i".repeat(64),
      supported: true,
      style: "prefix" as const,
      cache_id: null,
      model: null,
      created_at: null,
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: 1,
      per_item_artifact_messages: false,
    };
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You extract citations." },
        { role: "user", content: [{ type: "text", text: "Contract: stable reference clause." }] },
        { role: "user", content: "Find: renewal date" },
      ],
      outputSchema: schema,
      cachedSession,
    });
    const sent = requests[0]!.messages;
    expect(sent.map((m) => m.role)).toEqual(["system", "user", "user"]);
    expect(sent[1]!.content).toEqual([{ type: "text", text: "Contract: stable reference clause." }]);
    expect(sent[2]!.content).toBe("Find: renewal date");
  });

  it("reports the implicit cache hit via prompt_tokens_details.cached_tokens", async () => {
    const { transport } = usageTransport({
      prompt_tokens: 1200,
      completion_tokens: 9,
      total_tokens: 1209,
      prompt_tokens_details: { cached_tokens: 1024 },
    });
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    const seen: unknown[] = [];
    await provider.structuredCall({
      messages,
      outputSchema: schema,
      usageSink: (usage) => seen.push(usage),
    });
    expect(seen).toEqual([
      { model: "gpt-test", inputTokens: 1200, outputTokens: 9, totalTokens: 1209, cacheReadTokens: 1024 },
    ]);
  });

  it("a response without usage reports nothing", async () => {
    const { transport } = usageTransport();
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    const seen: unknown[] = [];
    await provider.structuredCall({
      messages,
      outputSchema: schema,
      usageSink: (usage) => seen.push(usage),
    });
    expect(seen).toEqual([]);
  });

  it("usage reports BEFORE the no-content throw (Python sink-before-extraction order)", async () => {
    const transport: OpenAIChatTransport = {
      chat: {
        completions: {
          create: () =>
            Promise.resolve({
              choices: [{ message: { content: null } }],
              usage: { prompt_tokens: 5, completion_tokens: 0 },
            }),
        },
      },
    };
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    const seen: unknown[] = [];
    await expect(
      provider.structuredCall({
        messages,
        outputSchema: schema,
        usageSink: (usage) => seen.push(usage),
      }),
    ).rejects.toThrow(/no message content/);
    expect(seen).toEqual([{ model: "gpt-test", inputTokens: 5, outputTokens: 0 }]);
  });
});

describe("OpenAIProvider truncation guard (#518)", () => {
  it("a finish_reason length response throws BEFORE reporting usage", async () => {
    const transport: OpenAIChatTransport = {
      chat: {
        completions: {
          create: () =>
            Promise.resolve({
              choices: [{ message: { content: '{"label": "cut' }, finish_reason: "length" }],
              usage: { prompt_tokens: 9, completion_tokens: 4096 },
            }),
        },
      },
    };
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    const seen: unknown[] = [];
    await expect(
      provider.structuredCall({
        messages,
        outputSchema: schema,
        usageSink: (usage) => seen.push(usage),
      }),
    ).rejects.toThrow(/truncated/);
    expect(seen).toEqual([]);
  });

  it("a normal finish_reason (or none) is untouched", async () => {
    const transport: OpenAIChatTransport = {
      chat: {
        completions: {
          create: () =>
            Promise.resolve({
              choices: [{ message: { content: JSON.stringify({ label: "ok" }) }, finish_reason: "stop" }],
            }),
        },
      },
    };
    const provider = new OpenAIProvider(transport, { model: "gpt-test" });
    await expect(provider.structuredCall({ messages, outputSchema: schema })).resolves.toEqual({ label: "ok" });
  });
});
