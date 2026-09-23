import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "../src/index.js";
import {
  AnthropicProvider,
  behaviorProviderParams,
  callOptions,
  defineActivity,
  executeActivity,
  GeminiProvider,
  InlinePromptRegistry,
  InMemoryCacheStore,
  mergeProviderParams,
  OpenAIProvider,
  prepareSessionCache,
  sessionCacheIdentity,
  stopProviderParam,
} from "../src/index.js";

const Input = z.object({ text: z.string() });
const Output = z.object({ label: z.string() });
const PROMPT = { name: "p/classify", label: "production" } as const;

class CapturingProvider implements ModelProvider {
  calls: StructuredCallParams[] = [];
  structuredCall(params: StructuredCallParams): unknown {
    this.calls.push(params);
    return { label: "ok" };
  }
}

describe("mergeProviderParams (#495, Python ProviderParams.merge)", () => {
  it("later layers win FIELD-WISE; null/undefined never erase", () => {
    expect(
      mergeProviderParams(
        { temperature: 0, top_p: 0.5, max_tokens: 100 },
        { temperature: 0.7, top_p: null, max_tokens: undefined },
        { max_tokens: 200 },
      ),
    ).toEqual({ temperature: 0.7, top_p: 0.5, max_tokens: 200 });
  });

  it("empty-string and zero values DO override (only None-likes are skipped)", () => {
    // Python merges non-None fields: "" and 0 are values, not absences.
    expect(mergeProviderParams({ model: "a", temperature: 1 }, { model: "", temperature: 0 })).toEqual({
      model: "",
      temperature: 0,
    });
  });

  it("an empty stop list never erases an earlier non-empty one (Python tuple-absence)", () => {
    expect(mergeProviderParams({ stop: ["END"] }, { stop: [] })).toEqual({ stop: ["END"] });
    expect(mergeProviderParams({ stop: [] })).toEqual({});
  });

  it("coerces a bare-string stop to a one-element list (Python from_mapping)", () => {
    expect(stopProviderParam({ stop: "END" })).toEqual(["END"]);
    expect(stopProviderParam({ stop: "" })).toBeUndefined();
  });

  it("tolerates absent layers and returns a fresh object", () => {
    const base = { temperature: 1 };
    const merged = mergeProviderParams(undefined, base, null);
    expect(merged).toEqual(base);
    expect(merged).not.toBe(base);
  });
});

describe("executeActivity params precedence (#495: defaults < prompt < activity)", () => {
  const registry = new InlinePromptRegistry({
    "p/classify": {
      ref: PROMPT,
      messages: [{ role: "user", content: "Classify {{ text }}" }],
      temperature: 0.3,
      providerParams: { top_p: 0.8, max_tokens: 500 },
      model: "prompt-model",
    },
  });

  it("merges call defaults < prompt temperature/params < activity params into the provider call", async () => {
    const provider = new CapturingProvider();
    const activity = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      providerParams: { max_tokens: 900 },
    });
    await executeActivity(activity, { text: "hi" }, {
      provider,
      registry,
      providerParams: { temperature: 0.1, seed: 7 },
    });
    expect(provider.calls[0]?.providerParams).toEqual({
      seed: 7, // call default survives (nothing overrode it)
      temperature: 0.3, // the prompt's dedicated temperature beats the call default
      top_p: 0.8, // prompt params layer
      max_tokens: 900, // activity params win over the prompt's 500
      model: "prompt-model", // the fold residue; the dedicated `model` field is authoritative
    });
    expect(provider.calls[0]?.model).toBe("prompt-model");
  });

  it("model precedence: options.model beats the prompt's; a params model is last and '' never selects", async () => {
    const provider = new CapturingProvider();
    const activity = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output });
    await executeActivity(activity, { text: "hi" }, { provider, registry, model: "explicit" });
    expect(provider.calls[0]?.model).toBe("explicit");

    const paramsOnly = new InlinePromptRegistry({
      "p/classify": {
        ref: PROMPT,
        messages: [{ role: "user", content: "Classify {{ text }}" }],
        providerParams: { model: "params-model" },
      },
    });
    await executeActivity(activity, { text: "hi" }, { provider, registry: paramsOnly });
    expect(provider.calls[1]?.model).toBe("params-model");

    const emptyModel = new InlinePromptRegistry({
      "p/classify": {
        ref: PROMPT,
        messages: [{ role: "user", content: "Classify {{ text }}" }],
        providerParams: { model: "" },
      },
    });
    // Python truthiness: an empty-string params model never selects a model.
    await executeActivity(activity, { text: "hi" }, { provider, registry: emptyModel });
    expect(provider.calls[2]?.model).toBeUndefined();

    // An ACTIVITY params model outranks the prompt's top-level model (codex #495
    // PR-A1: the model merges field-wise like every other param).
    const pinned = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      providerParams: { model: "activity-model" },
    });
    await executeActivity(pinned, { text: "hi" }, { provider, registry });
    expect(provider.calls[3]?.model).toBe("activity-model");
  });

  it("the pre-rendered messages path honors a params model for the CALL, not only the key", async () => {
    // Bugbot #526: a model inside the call/activity params must select the call's
    // model on the messages path too — never fold into the cache key while a
    // different model actually runs.
    const provider = new CapturingProvider();
    const activity = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output });
    await executeActivity(activity, { text: "hi" }, {
      provider,
      messages: [{ role: "user", content: "Classify: hi" }],
      providerParams: { model: "params-model" },
    });
    expect(provider.calls[0]?.model).toBe("params-model");
    // An explicit options.model still wins; an empty-string params model never selects.
    await executeActivity(activity, { text: "hi" }, {
      provider,
      messages: [{ role: "user", content: "Classify: hi" }],
      model: "explicit",
      providerParams: { model: "params-model" },
    });
    expect(provider.calls[1]?.model).toBe("explicit");
    await executeActivity(activity, { text: "hi" }, {
      provider,
      messages: [{ role: "user", content: "Classify: hi" }],
      providerParams: { model: "" },
    });
    expect(provider.calls[2]?.model).toBeUndefined();
  });

  it("the prompt's DEDICATED fields beat its own params record (Python with_legacy)", async () => {
    const provider = new CapturingProvider();
    const activity = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output });
    const conflicting = new InlinePromptRegistry({
      "p/classify": {
        ref: PROMPT,
        messages: [{ role: "user", content: "Classify {{ text }}" }],
        temperature: 0.3,
        model: "dedicated-model",
        providerParams: { temperature: 0.9, model: "record-model" },
      },
    });
    await executeActivity(activity, { text: "hi" }, { provider, registry: conflicting });
    expect(provider.calls[0]?.providerParams?.["temperature"]).toBe(0.3);
    expect(provider.calls[0]?.model).toBe("dedicated-model");
  });

  it("a prompt-level params change partitions the cross-run cache", async () => {
    const store = new InMemoryCacheStore();
    const cached = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output, cache: {} });
    const provider = new CapturingProvider();
    const registryAt = (temperature: number) =>
      new InlinePromptRegistry({
        "p/classify": {
          ref: PROMPT,
          messages: [{ role: "user", content: "Classify {{ text }}" }],
          temperature,
        },
      });
    await executeActivity(cached, { text: "hi" }, { provider, registry: registryAt(0.1), cacheStore: store });
    // Same params -> hit (no new provider call).
    await executeActivity(cached, { text: "hi" }, { provider, registry: registryAt(0.1), cacheStore: store });
    expect(provider.calls).toHaveLength(1);
    // Changed prompt temperature -> different key -> miss.
    await executeActivity(cached, { text: "hi" }, { provider, registry: registryAt(0.9), cacheStore: store });
    expect(provider.calls).toHaveLength(2);
  });
});

describe("provider request mapping (#495)", () => {
  const outputSchema = { type: "object", properties: { label: { type: "string" } } };
  const behavior = {
    temperature: 0.4,
    max_tokens: 700,
    top_p: 0.9,
    top_k: 40,
    seed: 11,
    frequency_penalty: 0.5,
    presence_penalty: -0.5,
    stop: ["END"],
    unknown_extra: "ignored",
    off_type: "not-a-number",
  };

  it("OpenAI forwards its supported fields and ignores the rest", async () => {
    const requests: Record<string, unknown>[] = [];
    const provider = new OpenAIProvider(
      {
        chat: {
          completions: {
            create: (request) => {
              requests.push(request as unknown as Record<string, unknown>);
              return Promise.resolve({ choices: [{ message: { content: JSON.stringify({ label: "ok" }) } }] });
            },
          },
        },
      },
      { model: "gpt-test" },
    );
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      providerParams: behavior,
    });
    expect(requests[0]).toMatchObject({
      temperature: 0.4,
      max_tokens: 700,
      top_p: 0.9,
      seed: 11,
      frequency_penalty: 0.5,
      presence_penalty: -0.5,
      stop: ["END"],
    });
    expect("top_k" in (requests[0] as object)).toBe(false); // not an OpenAI field
    expect("unknown_extra" in (requests[0] as object)).toBe(false);
  });

  it("Anthropic forwards its supported fields; params max_tokens overrides the cap", async () => {
    let request: Record<string, unknown> | undefined;
    const provider = new AnthropicProvider(
      {
        messages: {
          create: (req) => {
            request = req as unknown as Record<string, unknown>;
            return Promise.resolve({
              content: [{ type: "text", text: JSON.stringify({ label: "ok" }) }],
              stop_reason: "end_turn",
            });
          },
        },
      },
      { model: "claude-test" },
    );
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      providerParams: behavior,
    });
    expect(request).toMatchObject({
      max_tokens: 700,
      temperature: 0.4,
      top_p: 0.9,
      top_k: 40,
      stop_sequences: ["END"],
    });
    expect("seed" in (request as object)).toBe(false); // not an Anthropic field
  });

  it("Gemini forwards its supported fields into generationConfig", async () => {
    let request: { generationConfig: Record<string, unknown> } | undefined;
    const provider = new GeminiProvider(
      {
        generateContent: (req) => {
          request = req as unknown as { generationConfig: Record<string, unknown> };
          return Promise.resolve({
            candidates: [
              { content: { parts: [{ text: JSON.stringify({ label: "ok" }) }] }, finishReason: "STOP" },
            ],
          });
        },
      },
      { model: "gemini-test", maxOutputTokens: 100 },
    );
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      providerParams: behavior,
    });
    expect(request?.generationConfig).toMatchObject({
      maxOutputTokens: 700, // params override the constructor cap
      temperature: 0.4,
      topP: 0.9,
      topK: 40,
      stopSequences: ["END"],
    });
    // Python's Gemini provider refuses seed (unsupported there), so TS must not send it.
    expect("seed" in (request?.generationConfig ?? {})).toBe(false);
  });
});

describe("prepareSessionCache params merge (#495)", () => {
  it("the identity + provider prep see the SAME merged params as the fan-out will", async () => {
    const prepared: Record<string, unknown>[] = [];
    const provider: ModelProvider = {
      structuredCall: () => ({ label: "ok" }),
      supportsSessionCache: true,
      sessionCacheStyle: "prefix",
      prepareCachedSession: (params) => {
        prepared.push(params.providerParams ?? {});
        return {
          provider: "fake",
          identity_hash: params.identityHash,
          supported: true,
          style: "prefix",
          cache_id: null,
          model: null,
          created_at: null,
          ttl_seconds: null,
          reference_cached: false,
          prefix_stable_messages: null,
          per_item_artifact_messages: false,
        };
      },
    };
    const activity = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      sessionCache: {},
      providerParams: { max_tokens: 900 },
    });
    const registry = new InlinePromptRegistry({
      "p/classify": {
        ref: PROMPT,
        messages: [
          { role: "system", content: "You are careful." },
          { role: "user", content: "Classify {{ text }}" },
        ],
        providerParams: { top_p: 0.8 },
      },
    });
    const handle = await prepareSessionCache(activity, {
      provider,
      registry,
      createdAt: "2026-07-02T00:00:00+00:00",
      providerParams: { temperature: 0.2 },
    });
    expect(handle.supported).toBe(true);
    expect(prepared[0]).toEqual({ temperature: 0.2, top_p: 0.8, max_tokens: 900 });
    // The identity folds the merged params (a params change would re-identify).
    expect(handle.identity_hash).toBe(
      sessionCacheIdentity({
        providerName: "object",
        providerParams: { temperature: 0.2, top_p: 0.8, max_tokens: 900 },
        systemMessages: [{ role: "system", content: "You are careful." }],
        outputSchema: activity.outputProviderSchema,
      }),
    );
  });
});

describe("operational params + thinking budget (#495 A2)", () => {
  const outputSchema = { type: "object", properties: { label: { type: "string" } } };

  it("timeout is excluded from cache keys but reaches the transport as timeoutMs", async () => {
    const store = new InMemoryCacheStore();
    const cached = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output, cache: {} });
    const provider = new CapturingProvider();
    const base = { provider, messages: [{ role: "user", content: "Classify: hi" }], cacheStore: store };
    await executeActivity(cached, { text: "hi" }, { ...base, providerParams: { timeout: 30 } });
    // A DIFFERENT timeout must HIT the same entry (operational — not in the key).
    await executeActivity(cached, { text: "hi" }, { ...base, providerParams: { timeout: 99 } });
    expect(provider.calls).toHaveLength(1);
    // ...but the transport sees it: callOptions maps seconds -> ms.
    expect(callOptions({ messages: [], outputSchema, providerParams: { timeout: 30 } })).toEqual({
      timeoutMs: 30_000,
    });
    expect(callOptions({ messages: [], outputSchema })).toBeUndefined();
    expect(behaviorProviderParams({ timeout: 30, temperature: 0.5 })).toEqual({ temperature: 0.5 });
  });

  it("timeout does not re-identify a session cache; thinking_budget maps to Gemini thinkingConfig", async () => {
    const identityAt = (timeout: number) =>
      sessionCacheIdentity({
        providerName: "p",
        providerParams: behaviorProviderParams({ timeout, temperature: 0.5 }),
        systemMessages: [{ role: "system", content: "s" }],
      });
    expect(identityAt(30)).toBe(identityAt(99));

    let request: { generationConfig: Record<string, unknown> } | undefined;
    const gemini = new GeminiProvider(
      {
        generateContent: (req) => {
          request = req as unknown as { generationConfig: Record<string, unknown> };
          return Promise.resolve({
            candidates: [{ content: { parts: [{ text: JSON.stringify({ label: "ok" }) }] }, finishReason: "STOP" }],
          });
        },
      },
      { model: "gemini-test" },
    );
    // thinking_budget 0 is VALID: it disables thinking.
    await gemini.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema,
      providerParams: { thinking_budget: 0 },
    });
    expect(request?.generationConfig["thinkingConfig"]).toEqual({ thinkingBudget: 0 });
  });
});
