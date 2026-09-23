import { describe, expect, it, vi } from "vitest";

import type {
  ChatContent,
  GeminiGenerateContentRequest,
  GeminiGenerateContentResponse,
  GeminiGenerateContentTransport,
  JsonSchema,
} from "../src/index.js";
import { artifactRefSchema, GeminiProvider, ProviderCacheUnavailableError, ProviderConfigError } from "../src/index.js";

class FakeTransport implements GeminiGenerateContentTransport {
  lastRequest: GeminiGenerateContentRequest | undefined;
  constructor(
    private readonly response: GeminiGenerateContentResponse,
    private readonly error?: unknown,
  ) {}
  generateContent = async (request: GeminiGenerateContentRequest): Promise<GeminiGenerateContentResponse> => {
    this.lastRequest = request;
    if (this.error !== undefined) {
      throw this.error;
    }
    return this.response;
  };
}

const outputSchema: JsonSchema = {
  type: "object",
  properties: { summary: { type: "string" } },
  required: ["summary"],
  additionalProperties: false,
};

const textResponse = (json: unknown, finishReason = "STOP"): GeminiGenerateContentResponse => ({
  candidates: [{ content: { parts: [{ text: JSON.stringify(json) }] }, finishReason }],
});

describe("GeminiProvider (#449)", () => {
  it("builds a generateContent request with responseSchema and parses the JSON", async () => {
    const transport = new FakeTransport(textResponse({ summary: "done" }));
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });

    const out = await provider.structuredCall({
      messages: [{ role: "user", content: "summarize this" }],
      outputSchema,
    });

    expect(out).toEqual({ summary: "done" });
    expect(transport.lastRequest).toMatchObject({
      model: "gemini-2.5-flash",
      contents: [{ role: "user", parts: [{ text: "summarize this" }] }],
      generationConfig: { responseMimeType: "application/json", responseSchema: outputSchema },
    });
    expect("systemInstruction" in transport.lastRequest!).toBe(false);
  });

  it("maps assistant -> model and extracts system into systemInstruction", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });

    await provider.structuredCall({
      messages: [
        { role: "system", content: "You summarize." },
        { role: "system", content: "Be terse." },
        { role: "user", content: "go" },
        { role: "assistant", content: "ok" },
      ],
      outputSchema,
    });

    expect(transport.lastRequest!.systemInstruction).toEqual({
      parts: [{ text: "You summarize." }, { text: "Be terse." }],
    });
    expect(transport.lastRequest!.contents).toEqual([
      { role: "user", parts: [{ text: "go" }] },
      { role: "model", parts: [{ text: "ok" }] }, // assistant -> model
    ]);
  });

  it("uses the default model gemini-2.5-flash and respects maxOutputTokens", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport, { maxOutputTokens: 2048 });
    await provider.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema });
    expect(transport.lastRequest!.model).toBe("gemini-2.5-flash");
    expect(transport.lastRequest!.generationConfig.maxOutputTokens).toBe(2048);
  });

  // Transport error classification (transient/config/auth/truncation) is covered
  // for all providers in provider-errors.test.ts.

  it("throws on a system-only prompt (no user/model content for Gemini)", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport);
    await expect(
      provider.structuredCall({ messages: [{ role: "system", content: "be helpful" }], outputSchema }),
    ).rejects.toThrow(/at least one user or assistant message/);
  });

  it("throws on an unsupported message role", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport);
    await expect(
      provider.structuredCall({ messages: [{ role: "tool", content: "x" }], outputSchema }),
    ).rejects.toThrow(/unsupported message role/);
  });

  it("throws when the response has no content parts", async () => {
    const transport = new FakeTransport({ candidates: [{ content: { parts: [] }, finishReason: "STOP" }] });
    const provider = new GeminiProvider(transport);
    await expect(
      provider.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema }),
    ).rejects.toThrow(/no content parts/);
  });
});

describe("GeminiProvider content parts (#449)", () => {
  it("maps text parts to { text } and a `gemini` provider_extension to a native part", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });

    await provider.structuredCall({
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: "describe this image" },
            { type: "provider_extension", provider: "gemini", payload: { inlineData: { mimeType: "image/png", data: "AAAA" } } },
          ],
        },
      ],
      outputSchema,
    });

    // The text part and the provider extension's native payload both reach the request, in order.
    expect(transport.lastRequest!.contents).toEqual([
      {
        role: "user",
        parts: [{ text: "describe this image" }, { inlineData: { mimeType: "image/png", data: "AAAA" } }],
      },
    ]);
  });

  it("maps content parts in a system message into systemInstruction", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });

    await provider.structuredCall({
      messages: [
        { role: "system", content: [{ type: "text", text: "You summarize." }] },
        { role: "user", content: "go" },
      ],
      outputSchema,
    });

    expect(transport.lastRequest!.systemInstruction).toEqual({ parts: [{ text: "You summarize." }] });
  });

  it("does not mutate the caller's provider_extension payload", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    const payload = { inlineData: { mimeType: "image/png", data: "AAAA" } };

    await provider.structuredCall({
      messages: [{ role: "user", content: [{ type: "provider_extension", provider: "gemini", payload }] }],
      outputSchema,
    });

    // The request carries a copy, not the caller's object.
    const sentPart = transport.lastRequest!.contents[0]!.parts[0]!;
    expect(sentPart).toEqual(payload);
    expect(sentPart).not.toBe(payload);
  });

  it("fails loud on a provider_extension addressed to another provider", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport);
    const call = provider.structuredCall({
      messages: [{ role: "user", content: [{ type: "provider_extension", provider: "openai", payload: { x: 1 } }] }],
      outputSchema,
    });
    await expect(call).rejects.toThrow(/provider extension "openai" is not for Gemini/);
    // Typed as a config error (parity with Python's ProviderConfigError) so callers can catch it.
    await expect(call).rejects.toBeInstanceOf(ProviderConfigError);
  });

  it("fails loud on an artifact part whose group was not supplied (unknown artifact group)", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport);
    // No `artifacts` groups on the call: the lookup itself is the fail-loud guard (#481 PR2).
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: [{ type: "artifact", artifact: "photo", text: "a photo" }] }],
        outputSchema,
      }),
    ).rejects.toThrow(/unknown artifact group: photo/);
  });

  it("fails loud on a user message with empty content (no parts to send)", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport);
    await expect(
      provider.structuredCall({ messages: [{ role: "user", content: [] }], outputSchema }),
    ).rejects.toThrow(/empty content; Gemini requires at least one content part/);
    // The malformed request never reached the transport.
    expect(transport.lastRequest).toBeUndefined();
  });

  it("fails loud on an unknown content-part type (slips past the loose YAML loader, not `never`)", async () => {
    const transport = new FakeTransport(textResponse({ summary: "x" }));
    const provider = new GeminiProvider(transport);
    // The YAML loader validates content parts only as `{ type: string }`, so an unrecognized
    // type can reach the provider at runtime — it must throw, not return a malformed non-array part.
    const badContent = [{ type: "video", url: "x" }] as unknown as ChatContent;
    await expect(
      provider.structuredCall({ messages: [{ role: "user", content: badContent }], outputSchema }),
    ).rejects.toThrow(/unsupported content part type "video"/);
    // In a system message it must also fail loud (not a generic non-iterable TypeError on the spread).
    await expect(
      provider.structuredCall({
        messages: [{ role: "system", content: badContent }, { role: "user", content: "go" }],
        outputSchema,
      }),
    ).rejects.toThrow(/unsupported content part type "video"/);
  });
});

describe("GeminiProvider session cache (#478 PR5)", () => {
  const engaged = (over: Partial<import("../src/index.js").CachedSessionHandle> = {}) => ({
    provider: "gemini",
    identity_hash: "i".repeat(64),
    supported: true,
    style: "reference" as const,
    cache_id: "cachedContents/abc123",
    model: "gemini-2.5-flash",
    created_at: null,
    ttl_seconds: 600,
    reference_cached: false,
    prefix_stable_messages: null,
    per_item_artifact_messages: false,
    ...over,
  });

  /** A transport with a recording caches surface. */
  function cachesTransport(options: {
    response?: GeminiGenerateContentResponse;
    callError?: unknown;
    createResult?: { name?: string };
    createError?: unknown;
    deleteError?: unknown;
  } = {}): {
    transport: GeminiGenerateContentTransport;
    created: { model: string; config: Record<string, unknown> }[];
    deleted: { name: string }[];
    lastRequest: () => GeminiGenerateContentRequest | undefined;
  } {
    const created: { model: string; config: Record<string, unknown> }[] = [];
    const deleted: { name: string }[] = [];
    let last: GeminiGenerateContentRequest | undefined;
    const transport: GeminiGenerateContentTransport = {
      generateContent: async (request) => {
        last = request;
        if (options.callError !== undefined) {
          throw options.callError;
        }
        return options.response ?? textResponse({ summary: "done" });
      },
      caches: {
        create: async (params) => {
          created.push(params);
          if (options.createError !== undefined) {
            throw options.createError;
          }
          return options.createResult ?? { name: "cachedContents/abc123" };
        },
        delete: async (params) => {
          deleted.push(params);
          if (options.deleteError !== undefined) {
            throw options.deleteError;
          }
          return {};
        },
      },
    };
    return { transport, created, deleted, lastRequest: () => last };
  }

  it("prepareCachedSession uploads the prefix via caches.create (config shape + ttl string)", async () => {
    const { transport, created } = cachesTransport();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    const handle = await provider.prepareCachedSession({
      messages: [
        { role: "system", content: "You are careful." },
        { role: "user", content: "the stable prefix turn" },
      ],
      artifacts: [],
      identityHash: "i".repeat(64),
      ttlSeconds: 600,
    });
    expect(created).toEqual([
      {
        model: "gemini-2.5-flash",
        config: {
          // camelCase (JS genai SDK convention) — Python sends system_instruction to its own SDK.
          systemInstruction: { parts: [{ text: "You are careful." }] },
          contents: [{ role: "user", parts: [{ text: "the stable prefix turn" }] }],
          ttl: "600s",
        },
      },
    ]);
    expect(handle).toEqual({
      provider: "gemini",
      identity_hash: "i".repeat(64),
      supported: true,
      style: "reference",
      cache_id: "cachedContents/abc123",
      model: "gemini-2.5-flash",
      created_at: null,
      ttl_seconds: 600,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    });
  });

  it("prepare fails typed on: no caches surface, nothing stable, no returned name", async () => {
    const bare = new GeminiProvider(new FakeTransport(textResponse({ summary: "x" })), { model: "gemini-2.5-flash" });
    await expect(
      bare.prepareCachedSession({ messages: [{ role: "system", content: "s" }], artifacts: [], identityHash: "h" }),
    ).rejects.toThrow(/no `caches` surface/);

    const { transport } = cachesTransport();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    await expect(
      provider.prepareCachedSession({ messages: [], artifacts: [], identityHash: "h" }),
    ).rejects.toThrow(/needs a stable prefix/);

    const { transport: noName } = cachesTransport({ createResult: {} });
    const provider2 = new GeminiProvider(noName, { model: "gemini-2.5-flash" });
    await expect(
      provider2.prepareCachedSession({
        messages: [{ role: "system", content: "s" }],
        artifacts: [],
        identityHash: "h",
      }),
    ).rejects.toThrow(/returned no cache name/);
  });

  it("release deletes best-effort and swallows failures (TTL reaps it)", async () => {
    const { transport, deleted } = cachesTransport();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    await provider.releaseCachedSession(engaged());
    expect(deleted).toEqual([{ name: "cachedContents/abc123" }]);

    // A fallback handle (no cache_id) or a missing surface: nothing to release.
    await provider.releaseCachedSession(engaged({ cache_id: null }));
    expect(deleted).toHaveLength(1);
    const bare = new GeminiProvider(new FakeTransport(textResponse({ summary: "x" })), { model: "gemini-2.5-flash" });
    await expect(bare.releaseCachedSession(engaged())).resolves.toBeUndefined();

    // A delete failure is swallowed with a warning.
    const failing = cachesTransport({ deleteError: Object.assign(new Error("gone"), { status: 404 }) });
    const provider2 = new GeminiProvider(failing.transport, { model: "gemini-2.5-flash" });
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      await expect(provider2.releaseCachedSession(engaged())).resolves.toBeUndefined();
      expect(spy).toHaveBeenCalledOnce();
    } finally {
      spy.mockRestore();
    }
  });

  it("an engaged handle sends cachedContent and OMITS systemInstruction", async () => {
    const { transport, lastRequest } = cachesTransport();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are careful." },
        { role: "user", content: "per-item input" },
      ],
      outputSchema,
      cachedSession: engaged(),
    });
    expect(lastRequest()?.cachedContent).toBe("cachedContents/abc123");
    expect(lastRequest()?.systemInstruction).toBeUndefined();

    // A fail-soft handle sends the normal inline request.
    await provider.structuredCall({
      messages: [
        { role: "system", content: "You are careful." },
        { role: "user", content: "per-item input" },
      ],
      outputSchema,
      cachedSession: engaged({ supported: false, style: null, cache_id: null }),
    });
    expect(lastRequest()?.cachedContent).toBeUndefined();
    expect(lastRequest()?.systemInstruction).toEqual({ parts: [{ text: "You are careful." }] });
  });

  it("fails LOUD on a malformed engaged handle (foreign provider / no cache_id / model mismatch)", async () => {
    const { transport } = cachesTransport();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    const call = (cachedSession: ReturnType<typeof engaged>) =>
      provider.structuredCall({
        messages: [{ role: "user", content: "go" }],
        outputSchema,
        cachedSession,
      });
    await expect(call(engaged({ provider: "anthropic" }))).rejects.toThrow(/prepared by "anthropic"/);
    await expect(call(engaged({ cache_id: null }))).rejects.toThrow(/no cache_id/);
    await expect(call(engaged({ model: "gemini-other" }))).rejects.toThrow(/model-bound/);
  });

  it("classifies a stale-cache failure ONLY when a cachedContent was referenced", async () => {
    const staleError = Object.assign(new Error("403 PERMISSION_DENIED CachedContent not found"), { status: 403 });
    const { transport } = cachesTransport({ callError: staleError });
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: "go" }],
        outputSchema,
        cachedSession: engaged(),
      }),
    ).rejects.toBeInstanceOf(ProviderCacheUnavailableError);

    // 404 and the message-token variant classify too.
    const notFound = cachesTransport({ callError: Object.assign(new Error("gone"), { status: 404 }) });
    await expect(
      new GeminiProvider(notFound.transport, { model: "gemini-2.5-flash" }).structuredCall({
        messages: [{ role: "user", content: "go" }],
        outputSchema,
        cachedSession: engaged(),
      }),
    ).rejects.toBeInstanceOf(ProviderCacheUnavailableError);
    const tokenVariant = cachesTransport({ callError: new Error("CachedContent expired") });
    await expect(
      new GeminiProvider(tokenVariant.transport, { model: "gemini-2.5-flash" }).structuredCall({
        messages: [{ role: "user", content: "go" }],
        outputSchema,
        cachedSession: engaged(),
      }),
    ).rejects.toBeInstanceOf(ProviderCacheUnavailableError);

    // The SAME 403 without a cachedContent reference is NOT a stale-cache error.
    const uncached = new GeminiProvider(new FakeTransport(textResponse({ summary: "x" }), staleError), {
      model: "gemini-2.5-flash",
    });
    await expect(
      uncached.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema }),
    ).rejects.toThrow(/PERMISSION_DENIED/);
  });

  it("reports cachedContentTokenCount as cacheReadTokens (total forwarded verbatim)", async () => {
    const { transport } = cachesTransport({
      response: {
        ...textResponse({ summary: "done" }),
        usageMetadata: {
          promptTokenCount: 40,
          candidatesTokenCount: 8,
          totalTokenCount: 1100,
          cachedContentTokenCount: 1024,
        },
      },
    });
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    const seen: unknown[] = [];
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      cachedSession: engaged(),
      usageSink: (usage) => seen.push(usage),
    });
    expect(seen).toEqual([
      { model: "gemini-2.5-flash", inputTokens: 40, outputTokens: 8, totalTokens: 1100, cacheReadTokens: 1024 },
    ]);
  });

  it("a response without usageMetadata reports nothing", async () => {
    const provider = new GeminiProvider(new FakeTransport(textResponse({ summary: "done" })), {
      model: "gemini-2.5-flash",
    });
    const seen: unknown[] = [];
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      usageSink: (usage) => seen.push(usage),
    });
    expect(seen).toEqual([]);
  });
});

describe("GeminiProvider session cache — review edges (#478 PR5)", () => {
  const engaged2 = (over: Partial<import("../src/index.js").CachedSessionHandle> = {}) => ({
    provider: "gemini",
    identity_hash: "i".repeat(64),
    supported: true,
    style: "reference" as const,
    cache_id: "cachedContents/abc123",
    model: "gemini-2.5-flash",
    created_at: null,
    ttl_seconds: 600,
    reference_cached: false,
    prefix_stable_messages: null,
    per_item_artifact_messages: false,
    ...over,
  });

  function recordingCaches(): {
    transport: GeminiGenerateContentTransport;
    created: { model: string; config: Record<string, unknown> }[];
  } {
    const created: { model: string; config: Record<string, unknown> }[] = [];
    return {
      created,
      transport: {
        generateContent: async () => textResponse({ summary: "done" }),
        caches: {
          create: async (params) => {
            created.push(params);
            return { name: "cachedContents/abc123" };
          },
          delete: async () => ({}),
        },
      },
    };
  }

  it("omits the ttl key entirely when ttlSeconds is unset", async () => {
    const { transport, created } = recordingCaches();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    await provider.prepareCachedSession({
      messages: [{ role: "system", content: "stable" }],
      artifacts: [],
      identityHash: "h",
    });
    expect(created[0]?.config).toEqual({ systemInstruction: { parts: [{ text: "stable" }] } });
    expect("ttl" in (created[0]?.config ?? {})).toBe(false);
  });

  it("builds prefix contents from reference-artifact attachment parts", async () => {
    const { transport, created } = recordingCaches();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    const group = {
      name: "docs",
      artifacts: [
        {
          group: "docs",
          index: 0,
          ref: artifactRefSchema.parse({ source: { type: "url", url: "https://example.com/spec.pdf" } }),
          source_kind: "url" as const,
          kind: "document" as const,
          media_type: "application/pdf",
        },
      ],
    };
    await provider.prepareCachedSession({
      messages: [
        { role: "system", content: "stable" },
        { role: "user", content: [{ type: "artifact_group", group: "docs", text: "Reference document:" }] },
      ],
      artifacts: [group],
      identityHash: "h",
    });
    const contents = created[0]?.config["contents"] as { role: string; parts: unknown[] }[];
    expect(contents).toHaveLength(1);
    expect(contents[0]?.parts[0]).toEqual({ text: "Reference document:" });
    expect(contents[0]?.parts[1]).toEqual({
      fileData: { fileUri: "https://example.com/spec.pdf", mimeType: "application/pdf" },
    });
  });

  it("prepare throws typed ProviderConfigError shapes (instanceof pinned)", async () => {
    const { transport } = recordingCaches();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    await expect(
      provider.prepareCachedSession({ messages: [], artifacts: [], identityHash: "h" }),
    ).rejects.toBeInstanceOf(ProviderConfigError);
    await expect(
      provider.prepareCachedSession({
        messages: [
          { role: "system", content: "stable" },
          { role: "user", content: [] },
        ],
        artifacts: [],
        identityHash: "h",
      }),
    ).rejects.toThrow(/empty content/);
  });

  it("skips the model-mismatch check when the handle model is null or EMPTY (Python truthiness)", async () => {
    const { transport } = recordingCaches();
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    for (const model of [null, ""]) {
      const out = await provider.structuredCall({
        messages: [{ role: "user", content: "go" }],
        outputSchema,
        cachedSession: engaged2({ model }),
      });
      expect(out).toEqual({ summary: "done" });
    }
  });

  it("release treats an empty-string cache_id as nothing to release", async () => {
    const deleted: unknown[] = [];
    const transport: GeminiGenerateContentTransport = {
      generateContent: async () => textResponse({ summary: "x" }),
      caches: {
        create: async () => ({ name: "n" }),
        delete: async (params) => {
          deleted.push(params);
          return {};
        },
      },
    };
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    await provider.releaseCachedSession(engaged2({ cache_id: "" }));
    expect(deleted).toEqual([]);
  });

  it("usage reports BEFORE the no-content extraction throw (Python sink-before-extraction)", async () => {
    const transport: GeminiGenerateContentTransport = {
      generateContent: async () => ({
        candidates: [{ content: { parts: [] }, finishReason: "STOP" }],
        usageMetadata: { promptTokenCount: 5 },
      }),
    };
    const provider = new GeminiProvider(transport, { model: "gemini-2.5-flash" });
    const seen: unknown[] = [];
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: "go" }],
        outputSchema,
        usageSink: (usage) => seen.push(usage),
      }),
    ).rejects.toThrow(/no content parts/);
    expect(seen).toEqual([{ model: "gemini-2.5-flash", inputTokens: 5 }]);
  });
});
