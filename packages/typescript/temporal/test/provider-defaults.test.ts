import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import {
  AnthropicProvider, CollectingObserver, defineActivity, executeActivity, GeminiProvider,
  InlinePromptRegistry, InMemoryCacheStore, OpenAIProvider, prepareSessionCache,
  type ModelProvider, type StructuredCallParams,
} from "../src/index.js";

const messages = [{ role: "system", content: "Classify carefully." }, { role: "user", content: "Classify hi." }];
const prompt = { name: "classify" };
const activity = defineActivity({
  name: "classify", prompt, input: z.object({ text: z.string() }), output: z.object({ label: z.string() }),
  cache: {}, sessionCache: {},
});
const createdAt = "2026-10-25T00:00:00+00:00";
const input = { text: "hi" };

function fixture(kind: string) {
  const calls: string[] = [];
  const preparations: string[] = [];
  const content = (model: string) => {
    calls.push(model);
    return JSON.stringify({ label: model });
  };
  const provider = (model?: string): ModelProvider => {
    const options = model === undefined ? {} : { model };
    if (kind === "openai") return new OpenAIProvider({ chat: { completions: {
      create: async (request) => ({ choices: [{ message: { content: content(request.model) } }] }),
    } } }, options);
    if (kind === "anthropic") return new AnthropicProvider({ messages: {
      create: async (request) => ({ content: [{ type: "text", text: content(request.model) }], stop_reason: "end_turn" }),
    } }, options);
    return new GeminiProvider({
      generateContent: async (request) => ({ candidates: [{ content: { parts: [{ text: content(request.model) }] }, finishReason: "STOP" }] }),
      caches: {
        create: async (request) => { preparations.push(request.model); return { name: `cachedContents/${request.model}` }; },
        delete: async () => {},
      },
    }, options);
  };
  return { provider, calls, preparations };
}

describe.each(["openai", "anthropic", "gemini"])("%s constructor defaults", (kind) => {
  it("separates cached results and checks the effective model even on cache hits", async () => {
    const { provider, calls } = fixture(kind);
    const cacheStore = new InMemoryCacheStore();
    const guard = vi.fn();
    const observer = new CollectingObserver();
    for (const model of ["model-a", "model-a", "model-b", "model-b"]) {
      expect(await executeActivity(activity, input, {
        provider: provider(model), messages, cacheStore, providerModelGuard: guard, observer,
      })).toEqual({ label: model });
    }
    expect(calls).toEqual(["model-a", "model-b"]);
    expect(guard.mock.calls.map(([value]) => value.model)).toEqual(["model-a", "model-a", "model-b", "model-b"]);
    expect(observer.activities.map((value) => value.model)).toEqual(["model-a", "model-a", "model-b", "model-b"]);
    expect(observer.activities.flatMap((value) => value.generations.map((generation) => generation.model))).toEqual(["model-a", "model-b"]);
    await expect(executeActivity(activity, input, {
      provider: provider("model-a"), messages, cacheStore,
      providerModelGuard: ({ model }) => { if (model === "model-a") throw new Error("model denied"); },
    })).rejects.toThrow("model denied");
    expect(calls).toEqual(["model-a", "model-b"]);
  });

  it("preserves call defaults, prompt, activity and explicit model precedence", async () => {
    const { provider } = fixture(kind);
    const registry = new InlinePromptRegistry({ classify: { ref: prompt, messages, model: "prompt-model" } });
    const pinned = defineActivity({ ...activity, providerParams: { model: "activity-model" } });
    for (const [descriptor, options, expected] of [
      [activity, { messages }, "constructor-model"],
      [activity, { messages, providerParams: { model: "" } }, "constructor-model"],
      [activity, { registry: new InlinePromptRegistry({ classify: { ref: prompt, messages, model: "" } }) }, "constructor-model"],
      [activity, { messages, providerParams: { model: "call-default" } }, "call-default"],
      [activity, { registry, providerParams: { model: "call-default" } }, "prompt-model"],
      [pinned, { registry, providerParams: { model: "call-default" } }, "activity-model"],
      [pinned, { registry, model: "explicit-model" }, "explicit-model"],
      [pinned, { messages, model: "explicit-model" }, "explicit-model"],
    ] as const) {
      const configured = provider("constructor-model");
      const guard = vi.fn();
      expect(await executeActivity(descriptor, input, { provider: configured, ...options, providerModelGuard: guard })).toEqual({ label: expected });
      const handle = await prepareSessionCache(descriptor, { createdAt, provider: configured, ...options, providerModelGuard: guard });
      expect(handle.model).toBe(expected);
      expect(guard.mock.calls.map(([value]) => value.model)).toEqual([expected, expected]);
    }
  });

  async function checkSessionPreparation(source: "messages" | "registry") {
    const { provider, preparations } = fixture(kind);
    const guard = vi.fn();
    const registry = new InlinePromptRegistry({ classify: { ref: prompt, messages } });
    const handles = [];
    for (const model of ["model-a", "model-b"]) {
      handles.push(await prepareSessionCache(activity, {
        createdAt, provider: provider(model), ...(source === "messages" ? { messages } : { registry }), providerModelGuard: guard,
      }));
    }
    expect(handles.map((handle) => handle.model)).toEqual(["model-a", "model-b"]);
    expect(handles.every((handle) => handle.supported)).toBe(true);
    expect(handles[0]!.identity_hash).not.toBe(handles[1]!.identity_hash);
    expect(guard.mock.calls.map(([value]) => value.model)).toEqual(["model-a", "model-b"]);
    if (kind === "gemini") expect(preparations).toEqual(["model-a", "model-b"]);
    await expect(prepareSessionCache(activity, {
      createdAt, provider: provider("model-a"), messages,
      providerModelGuard: ({ model }) => { if (model === "model-a") throw new Error("prep denied"); },
    })).rejects.toThrow("prep denied");
    if (kind === "gemini") expect(preparations).toHaveLength(2);
  }
  it("prepares distinct model-bound sessions through messages", () => checkSessionPreparation("messages"));
  it("prepares distinct model-bound sessions through registry", () => checkSessionPreparation("registry"));
});

it("exposes Gemini's implicit model to policy and observations", async () => {
  const { provider } = fixture("gemini");
  const guard = vi.fn();
  const observer = new CollectingObserver();
  await executeActivity(activity, input, { provider: provider(), messages, providerModelGuard: guard, observer });
  expect(guard.mock.calls[0]?.[0].model).toBe("gemini-2.5-flash");
  expect(observer.activities[0]?.model).toBe("gemini-2.5-flash");
});

it("preserves undefined models for custom providers without declared defaults", async () => {
  const calls: StructuredCallParams[] = [];
  const provider: ModelProvider = { structuredCall: (params) => { calls.push(params); return { label: "custom" }; } };
  const guard = vi.fn();
  const observer = new CollectingObserver();
  await executeActivity(activity, input, { provider, messages, providerModelGuard: guard, observer });
  const handle = await prepareSessionCache(activity, { createdAt, provider, messages, providerModelGuard: guard });
  expect(calls[0]?.model).toBeUndefined();
  expect(calls[0]?.providerParams).toBeUndefined();
  expect(guard.mock.calls.map(([value]) => value.model)).toEqual([undefined, undefined]);
  expect(observer.activities[0]?.model).toBeNull();
  expect(handle.model).toBeNull();
});
