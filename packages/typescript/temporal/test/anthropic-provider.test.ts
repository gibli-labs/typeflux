import { describe, expect, it } from "vitest";

import type {
  AnthropicMessagesRequest,
  AnthropicMessagesResponse,
  AnthropicMessagesTransport,
  JsonSchema,
} from "../src/index.js";
import { AnthropicProvider, artifactRefSchema, ProviderConfigError } from "../src/index.js";

class FakeTransport implements AnthropicMessagesTransport {
  lastRequest: AnthropicMessagesRequest | undefined;
  calls = 0;
  constructor(
    private readonly response: AnthropicMessagesResponse,
    private readonly error?: unknown,
  ) {}
  messages = {
    create: async (request: AnthropicMessagesRequest): Promise<AnthropicMessagesResponse> => {
      this.calls += 1;
      this.lastRequest = request;
      if (this.error !== undefined) {
        throw this.error;
      }
      return this.response;
    },
  };
}

const outputSchema: JsonSchema = {
  type: "object",
  properties: { summary: { type: "string" } },
  required: ["summary"],
  additionalProperties: false,
};

const textResponse = (json: unknown, stop = "end_turn"): AnthropicMessagesResponse => ({
  content: [{ type: "text", text: JSON.stringify(json) }],
  stop_reason: stop,
});

describe("AnthropicProvider (#449)", () => {
  it("builds a structured-output request and parses the JSON text block", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });

    const out = await provider.structuredCall({
      messages: [{ role: "user", content: "summarize this" }],
      outputSchema,
    });

    expect(out).toEqual({ summary: "done" });
    expect(transport.lastRequest).toMatchObject({
      model: "claude-sonnet-4-6",
      max_tokens: 4096,
      messages: [{ role: "user", content: "summarize this" }],
      output_config: { format: { type: "json_schema", schema: outputSchema } },
    });
    expect("system" in transport.lastRequest!).toBe(false);
  });

  it("extracts system-role messages into the top-level system field", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });

    await provider.structuredCall({
      messages: [
        { role: "system", content: "You summarize." },
        { role: "system", content: "Be terse." },
        { role: "user", content: "go" },
      ],
      outputSchema,
    });

    expect(transport.lastRequest!.system).toBe("You summarize.\n\nBe terse.");
    expect(transport.lastRequest!.messages).toEqual([{ role: "user", content: "go" }]);
  });

  it("joins text-bearing system parts and rejects non-text system content (Python parity)", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    // A non-text system part is unrepresentable in Anthropic's string system prompt — it must
    // fail loud, not silently vanish (the old contentToText dropped it).
    await expect(
      provider.structuredCall({
        messages: [
          { role: "system", content: [{ type: "provider_extension", provider: "anthropic", payload: {} }] },
          { role: "user", content: "go" },
        ],
        outputSchema,
      }),
    ).rejects.toThrow(/only supports text content in system messages/);
    await provider.structuredCall({
      messages: [
        {
          role: "system",
          content: [
            { type: "text", text: "Follow the policy." },
            { type: "artifact", artifact: "doc-1", text: "Cite exhibit A." },
          ],
        },
        { role: "user", content: "go" },
      ],
      outputSchema,
    });
    expect(transport.lastRequest!.system).toBe("Follow the policy.\n\nCite exhibit A.");
  });

  it("ignores a null parsed_output and falls back to the JSON text block", async () => {
    const transport = new FakeTransport({
      content: [{ type: "text", text: JSON.stringify({ summary: "from-text" }), parsed_output: null }],
      parsed_output: null,
      stop_reason: "end_turn",
    });
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    expect(await provider.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema })).toEqual({
      summary: "from-text",
    });
  });

  it("returns parsed_output directly when present (SDK .parse())", async () => {
    const transport = new FakeTransport({
      content: [{ type: "text", text: "{}", parsed_output: { summary: "from-block" } }],
      parsed_output: { summary: "top-level" },
      stop_reason: "end_turn",
    });
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    expect(await provider.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema })).toEqual({
      summary: "top-level",
    });
  });

  // Transport error classification (transient/config/auth/truncation) is covered
  // for all providers in provider-errors.test.ts.

  it("requires a model and a positive maxTokens", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    await expect(
      new AnthropicProvider(transport).structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema }),
    ).rejects.toThrow(/a model is required/);
    expect(() => new AnthropicProvider(transport, { maxTokens: 0 })).toThrow(/maxTokens must be >= 1/);
  });

  it("throws when the text block is not valid JSON", async () => {
    const transport = new FakeTransport({ content: [{ type: "text", text: "not json" }], stop_reason: "end_turn" });
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await expect(
      provider.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema }),
    ).rejects.toThrow(/not valid JSON/);
  });
});

describe("AnthropicProvider config errors (#493)", () => {
  it("throws a typed ProviderConfigError for an invalid maxTokens", () => {
    expect(() => new AnthropicProvider({ messages: { create: async () => ({}) } } as never, { maxTokens: 0 })).toThrow(
      ProviderConfigError,
    );
  });
});

describe("AnthropicProvider role guard (#493)", () => {
  const outputSchema = { type: "object" as const, properties: {}, additionalProperties: false };

  it("throws a typed ProviderConfigError for an unsupported message role", async () => {
    const provider = new AnthropicProvider(
      { messages: { create: async () => ({ content: [] }) } } as never,
      { model: "claude-sonnet-4-6" },
    );
    await expect(
      provider.structuredCall({ messages: [{ role: "tool", content: "x" }], outputSchema }),
    ).rejects.toBeInstanceOf(ProviderConfigError);
  });
});

describe("AnthropicProvider session cache (#478 PR4)", () => {
  const engaged = (identityHash: string) => ({
    provider: "anthropic",
    identity_hash: identityHash,
    supported: true,
    style: "prefix" as const,
    cache_id: null,
    model: null,
    created_at: null,
    ttl_seconds: null,
    reference_cached: false,
    prefix_stable_messages: null,
    per_item_artifact_messages: false,
  });

  it("declares the prefix capability and prepares WITHOUT touching the transport", () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    expect(provider.supportsSessionCache).toBe(true);
    expect(provider.sessionCacheStyle).toBe("prefix");
    const handle = provider.prepareCachedSession({
      messages: [{ role: "system", content: "stable" }],
      artifacts: [],
      identityHash: "i".repeat(64),
      ttlSeconds: 600,
    });
    expect(transport.calls).toBe(0);
    expect(handle).toEqual({
      provider: "anthropic",
      identity_hash: "i".repeat(64),
      supported: true,
      style: "prefix",
      cache_id: null,
      model: "claude-sonnet-4-6",
      created_at: null,
      // The requested TTL is never sent for prefix style, so none is recorded.
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    });
    expect(() => provider.releaseCachedSession()).not.toThrow();
  });

  it("marks system + second-to-last-message breakpoints when the handle is ENGAGED", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are careful." },
        { role: "user", content: "the stable prefix turn" },
        { role: "user", content: "per-item input" },
      ],
      outputSchema,
      cachedSession: engaged("i".repeat(64)),
    });
    expect(transport.lastRequest?.system).toEqual([
      { type: "text", text: "You are careful.", cache_control: { type: "ephemeral" } },
    ]);
    // String content of the SECOND-TO-LAST message becomes a single marked text block.
    expect(transport.lastRequest?.messages[0]?.content).toEqual([
      { type: "text", text: "the stable prefix turn", cache_control: { type: "ephemeral" } },
    ]);
    // The final (per-item) message is untouched.
    expect(transport.lastRequest?.messages[1]?.content).toBe("per-item input");
  });

  it("marks the LAST CACHEABLE block, skipping a trailing non-cacheable one", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: "stable docs" },
            { type: "provider_extension", provider: "anthropic", payload: { type: "web_search_result" } },
          ],
        },
        { role: "user", content: "per-item input" },
      ],
      outputSchema,
      cachedSession: engaged("i".repeat(64)),
    });
    const blocks = transport.lastRequest?.messages[0]?.content as Record<string, unknown>[];
    expect(blocks[0]).toMatchObject({ type: "text", text: "stable docs", cache_control: { type: "ephemeral" } });
    expect("cache_control" in (blocks[1] as Record<string, unknown>)).toBe(false);
  });

  it("a single-message conversation marks only the system block", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are careful." },
        { role: "user", content: "per-item input" },
      ],
      outputSchema,
      cachedSession: engaged("i".repeat(64)),
    });
    expect(Array.isArray(transport.lastRequest?.system)).toBe(true);
    expect(transport.lastRequest?.messages[0]?.content).toBe("per-item input");
  });

  it("#362: prefix_stable_messages=k>0 marks conversation[k-1], not the -2 default", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are precise." },
        { role: "user", content: "Contract text (stable reference)." }, // k-1 == 0
        { role: "user", content: "the per-item question" }, // variable input
      ],
      outputSchema,
      cachedSession: { ...engaged("i".repeat(64)), prefix_stable_messages: 1 },
    });
    expect(transport.lastRequest?.messages[0]?.content).toEqual([
      { type: "text", text: "Contract text (stable reference).", cache_control: { type: "ephemeral" } },
    ]);
    expect(transport.lastRequest?.messages[1]?.content).toBe("the per-item question");
  });

  it("#362: a count that would hit the final message clamps back to conversation[-2]", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are precise." },
        { role: "user", content: "stable" },
        { role: "user", content: "per-item" },
      ],
      outputSchema,
      // k=2, len(conversation)=2: k-1 is the final message, so clamp to index 0.
      cachedSession: { ...engaged("i".repeat(64)), prefix_stable_messages: 2 },
    });
    expect(transport.lastRequest?.messages[0]?.content).toEqual([
      { type: "text", text: "stable", cache_control: { type: "ephemeral" } },
    ]);
    expect(transport.lastRequest?.messages[1]?.content).toBe("per-item"); // final untouched
  });

  it("#698: per-item artifact shape marks only the system block, not the varying query", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are precise." },
        { role: "user", content: "the per-item question" }, // varies per item
        { role: "user", content: "per-item artifact attach" }, // trailing attach
      ],
      outputSchema,
      // No reference span; per-item artifact trails the varying turn → no stable
      // conversation span, so the conversation is left unmarked.
      cachedSession: { ...engaged("i".repeat(64)), per_item_artifact_messages: true },
    });
    // System block still caches (the shape's remaining benefit).
    expect(transport.lastRequest?.system).toEqual([
      { type: "text", text: "You are precise.", cache_control: { type: "ephemeral" } },
    ]);
    // Neither conversation turn is marked — both vary.
    expect(transport.lastRequest?.messages[0]?.content).toBe("the per-item question");
    expect(transport.lastRequest?.messages[1]?.content).toBe("per-item artifact attach");
    expect(JSON.stringify(transport.lastRequest?.messages)).not.toContain("cache_control");
  });

  it("#698: the per-item artifact flag is ignored when a reference span is present", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are precise." },
        { role: "user", content: "Contract text (stable reference)." }, // conversation[0]
        { role: "user", content: "the per-item question" }, // varies
        { role: "user", content: "per-item artifact attach" }, // trailing attach
      ],
      outputSchema,
      // k>0 is authoritative: the reference span is marked, flag notwithstanding.
      cachedSession: {
        ...engaged("i".repeat(64)),
        prefix_stable_messages: 1,
        per_item_artifact_messages: true,
      },
    });
    expect(transport.lastRequest?.messages[0]?.content).toEqual([
      { type: "text", text: "Contract text (stable reference).", cache_control: { type: "ephemeral" } },
    ]);
    expect(transport.lastRequest?.messages[1]?.content).toBe("the per-item question");
    expect(transport.lastRequest?.messages[2]?.content).toBe("per-item artifact attach");
  });

  it("#698: the default-false flag keeps the legacy conversation[-2] contract", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are precise." },
        { role: "user", content: "instructions turn" }, // -2 (legacy)
        { role: "user", content: "per-item" },
      ],
      outputSchema,
      cachedSession: engaged("i".repeat(64)), // per_item_artifact_messages defaults false
    });
    expect(transport.lastRequest?.messages[0]?.content).toEqual([
      { type: "text", text: "instructions turn", cache_control: { type: "ephemeral" } },
    ]);
    expect(transport.lastRequest?.messages[1]?.content).toBe("per-item");
  });

  it("#362 documented outcome: multi-turn marks only the reference span", async () => {
    // [system, reference, stable_user, per_item] with k=1 marks conversation[0]
    // (the reference); a later stable turn is NOT covered by the breakpoint —
    // Anthropic caches up to and including the marked block only.
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are precise." },
        { role: "user", content: "Reference contract clause." },
        { role: "user", content: "Shared rubric turn." },
        { role: "user", content: "the per-item question" },
      ],
      outputSchema,
      cachedSession: { ...engaged("i".repeat(64)), prefix_stable_messages: 1 },
    });
    expect(transport.lastRequest?.messages[0]?.content).toEqual([
      { type: "text", text: "Reference contract clause.", cache_control: { type: "ephemeral" } },
    ]);
    expect(transport.lastRequest?.messages[1]?.content).toBe("Shared rubric turn.");
    expect(transport.lastRequest?.messages[2]?.content).toBe("the per-item question");
  });

  it("#362: a system-role text reference is cached in the system block; a bare document raises", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    // Text reference folds into the cached system block (works).
    await provider.structuredCall({
      messages: [
        {
          role: "system",
          content: [
            { type: "text", text: "You are precise." },
            { type: "text", text: "Reference: contract clause." },
          ],
        },
        { role: "user", content: "per-item" },
      ],
      outputSchema,
      cachedSession: { ...engaged("i".repeat(64)), prefix_stable_messages: 0 },
    });
    expect(transport.lastRequest?.system).toEqual([
      {
        type: "text",
        text: "You are precise.\n\nReference: contract clause.",
        cache_control: { type: "ephemeral" },
      },
    ]);
    // A document (artifact_group with no text) in a system message has nothing to
    // fold and cannot ride the text-only system block — it raises.
    await expect(
      provider.structuredCall({
        messages: [
          { role: "system", content: [{ type: "artifact_group", group: "contract" }] },
          { role: "user", content: "q" },
        ],
        artifacts: [
          {
            name: "contract",
            artifacts: [
              {
                group: "contract",
                index: 0,
                ref: artifactRefSchema.parse({ source: "contract.pdf" }),
                source_kind: "local_path",
                kind: "document",
                media_type: "application/pdf",
                sha256: "e".repeat(64),
                size_bytes: 64,
              },
            ],
          },
        ],
        outputSchema,
      }),
    ).rejects.toThrow(/only supports text content in system messages/);
  });

  it("a fail-soft handle (or none) leaves the request unmarked", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    const messages = [
      { role: "system", content: "You are careful." },
      { role: "user", content: "prefix" },
      { role: "user", content: "item" },
    ];
    await provider.structuredCall({
      messages,
      outputSchema,
      cachedSession: { ...engaged("i".repeat(64)), supported: false, style: null },
    });
    expect(transport.lastRequest?.system).toBe("You are careful.");
    expect(transport.lastRequest?.messages[0]?.content).toBe("prefix");
    await provider.structuredCall({ messages, outputSchema });
    expect(transport.lastRequest?.system).toBe("You are careful.");
  });

  it("reports cache read/write tokens through the usage sink (input_tokens is the UNCACHED input)", async () => {
    const transport = new FakeTransport({
      ...textResponse({ summary: "done" }),
      usage: { input_tokens: 12, output_tokens: 7, cache_read_input_tokens: 900, cache_creation_input_tokens: 0 },
    });
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    const seen: unknown[] = [];
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      usageSink: (usage) => seen.push(usage),
    });
    expect(seen).toEqual([
      { model: "claude-sonnet-4-6", inputTokens: 12, outputTokens: 7, cacheReadTokens: 900, cacheWriteTokens: 0 },
    ]);
  });

  it("a response without usage reports nothing", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    const seen: unknown[] = [];
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      usageSink: (usage) => seen.push(usage),
    });
    expect(seen).toEqual([]);
  });

  it("a truncated response throws BEFORE reporting usage (Python _validated_provider_result order)", async () => {
    const transport = new FakeTransport({
      ...textResponse({ summary: "done" }, "max_tokens"),
      usage: { input_tokens: 12, output_tokens: 7, cache_read_input_tokens: 900 },
    });
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    const seen: unknown[] = [];
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: "go" }],
        outputSchema,
        usageSink: (usage) => seen.push(usage),
      }),
    ).rejects.toThrow(/truncated/);
    expect(seen).toEqual([]);
  });

  it("multiple system messages join into ONE marked block (not one block per message)", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are careful." },
        { role: "system", content: "Follow the rubric." },
        { role: "user", content: "per-item input" },
      ],
      outputSchema,
      cachedSession: engaged("i".repeat(64)),
    });
    expect(transport.lastRequest?.system).toEqual([
      { type: "text", text: "You are careful.\n\nFollow the rubric.", cache_control: { type: "ephemeral" } },
    ]);
  });
});
