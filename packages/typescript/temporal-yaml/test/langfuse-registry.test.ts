/**
 * Out-of-the-box langfuse prompt registry (`langfuse-registry.ts`): the SDK
 * transport mapping (chat/text prompts, version-vs-label selection, config
 * model/temperature/provider_params) and the spec/env gate `buildRuntime`
 * calls. The SDK client is faked structurally (`LangfuseRegistrySdkClient`);
 * the real `langfuse` package is only touched by the construct-only test (no
 * prompt is fetched — the network guard would block it anyway). Env is always
 * passed EXPLICITLY: the shared network guard scrubs LANGFUSE_* from unit-test
 * env, so ambient reads would see nothing.
 */

import { describe, expect, it } from "vitest";

import {
  PromptNotFoundError,
  PromptRegistryConfigError,
  PromptRegistryUnavailableError,
  TransportPromptRegistry,
} from "@typeflux/temporal";
import type { PromptRef } from "@typeflux/temporal";

import {
  langfuseRegistryTransportFromSpec,
  LangfuseRegistryTransport,
  loadYamlSpec,
  registryFromSpec,
} from "../src/index.js";
import type { LangfuseRegistryPromptClient } from "../src/index.js";

const YAML = `
project: reg_demo
name: reg_demo
task_queue: reg-demo
runtime:
  temporal:
    address: localhost:7233
  registry:
    type: langfuse
  provider:
    type: openai
activities:
  definitions:
    - name: act
      input: schemas:Item
      output: schemas:Item
      prompt: p
workflow:
  name: RegDemoWorkflow
  input: schemas:Item
  output: schemas:Item
  steps:
    - id: act
      activity: act
`;

function specWithRegistry(type: string, extra = "") {
  return loadYamlSpec(YAML.replace("    type: langfuse", `    type: ${type}${extra}`));
}

const KEYS = { LANGFUSE_PUBLIC_KEY: "pk-test", LANGFUSE_SECRET_KEY: "sk-test" };

class FakeClient {
  calls: { name: string; version?: number | undefined; label?: string | undefined }[] = [];
  constructor(private readonly result: LangfuseRegistryPromptClient) {}

  async getPrompt(
    name: string,
    version?: number,
    options?: { label?: string },
  ): Promise<LangfuseRegistryPromptClient> {
    this.calls.push({ name, version, label: options?.label });
    return this.result;
  }
}

const CHAT_PROMPT: LangfuseRegistryPromptClient = {
  type: "chat",
  prompt: [
    { type: "chatmessage", role: "system", content: "you are {{persona}}" },
    { role: "user", content: "hi {{name}}" },
  ],
  version: 3,
  config: { model: "gpt-x", temperature: 0.2, provider_params: { top_p: 0.9 } },
};

describe("LangfuseRegistryTransport", () => {
  it("maps a chat prompt to messages + config model/temperature/params + version", async () => {
    const transport = new LangfuseRegistryTransport(new FakeClient(CHAT_PROMPT));
    const raw = await transport.fetchPrompt({ name: "p" });
    expect(raw.messages).toEqual([
      { role: "system", content: "you are {{persona}}" },
      { role: "user", content: "hi {{name}}" },
    ]);
    expect(raw.model).toBe("gpt-x");
    expect(raw.temperature).toBe(0.2);
    expect(raw.providerParams).toEqual({ top_p: 0.9 });
    expect(raw.resolvedVersion).toBe("3");
  });

  it("maps a text prompt to a single user message (Python parity)", async () => {
    const transport = new LangfuseRegistryTransport(
      new FakeClient({ type: "text", prompt: "summarize {{doc}}", version: 7, config: {} }),
    );
    const raw = await transport.fetchPrompt({ name: "p" });
    expect(raw.messages).toEqual([{ role: "user", content: "summarize {{doc}}" }]);
    expect(raw.model).toBeUndefined();
    expect(raw.resolvedVersion).toBe("7");
  });

  it("selects by version when pinned, by label otherwise, defaulting label to production", async () => {
    const client = new FakeClient(CHAT_PROMPT);
    const transport = new LangfuseRegistryTransport(client);
    await transport.fetchPrompt({ name: "p", version: 4 });
    await transport.fetchPrompt({ name: "p", label: "canary" });
    await transport.fetchPrompt({ name: "p" });
    expect(client.calls).toEqual([
      { name: "p", version: 4, label: undefined },
      { name: "p", version: undefined, label: "canary" },
      // Python parity: no version and no label resolves the production label.
      { name: "p", version: undefined, label: "production" },
    ]);
  });

  it("rejects a promptType hint that contradicts the resolved type with Python's texts", async () => {
    const chat = new LangfuseRegistryTransport(new FakeClient(CHAT_PROMPT));
    await expect(chat.fetchPrompt({ name: "p", promptType: "text" })).rejects.toThrow(
      "resolved prompt p@default-label did not contain text content",
    );
    const text = new LangfuseRegistryTransport(
      new FakeClient({ type: "text", prompt: "t", version: 1 }),
    );
    await expect(text.fetchPrompt({ name: "p", label: "canary", promptType: "chat" })).rejects.toThrow(
      "resolved prompt p@canary did not contain chat messages",
    );
  });

  it("rejects malformed chat messages loud (role gate, content shape, name shape)", async () => {
    const placeholder = new LangfuseRegistryTransport(
      new FakeClient({ type: "chat", prompt: [{ type: "placeholder", name: "history" }], version: 1 }),
    );
    // A langfuse placeholder entry has no role — Python's role gate fires.
    await expect(placeholder.fetchPrompt({ name: "p" })).rejects.toThrow(
      "resolved chat prompt p@default-label message 0 role must be system, user, or assistant",
    );
    const badContent = new LangfuseRegistryTransport(
      new FakeClient({ type: "chat", prompt: [{ role: "user", content: 42 }], version: 1 }),
    );
    await expect(badContent.fetchPrompt({ name: "p" })).rejects.toThrow(
      "resolved chat prompt p@default-label message 0 content must be text or Typeflux content parts",
    );
    const empty = new LangfuseRegistryTransport(new FakeClient({ type: "chat", prompt: [], version: 1 }));
    await expect(empty.fetchPrompt({ name: "p" })).rejects.toThrow(
      "resolved prompt p@default-label did not contain chat messages",
    );
  });

  it("accepts Typeflux-shaped content parts as chat content", async () => {
    const parts = [{ type: "text", text: "see {{doc}}" }, { type: "artifact", artifact: "doc-1" }];
    const transport = new LangfuseRegistryTransport(
      new FakeClient({ type: "chat", prompt: [{ role: "user", content: parts }], version: 1 }),
    );
    const raw = await transport.fetchPrompt({ name: "p" });
    expect(raw.messages).toEqual([{ role: "user", content: parts }]);
  });

  it("rejects malformed config with Python's texts", async () => {
    const badModel = new LangfuseRegistryTransport(
      new FakeClient({ type: "text", prompt: "t", version: 1, config: { model: 5 } }),
    );
    await expect(badModel.fetchPrompt({ name: "p" })).rejects.toThrow(
      "resolved prompt p@default-label model must be a string",
    );
    const badTemp = new LangfuseRegistryTransport(
      new FakeClient({ type: "text", prompt: "t", version: 1, config: { temperature: "hot" } }),
    );
    await expect(badTemp.fetchPrompt({ name: "p" })).rejects.toThrow(
      "resolved prompt p@default-label temperature must be numeric",
    );
    const mismatch = new LangfuseRegistryTransport(
      new FakeClient({
        type: "text",
        prompt: "t",
        version: 1,
        config: { model: "a", provider_params: { model: "b" } },
      }),
    );
    await expect(mismatch.fetchPrompt({ name: "p" })).rejects.toThrow(
      "provider_params invalid: model and provider_params.model must match",
    );
    const badParams = new LangfuseRegistryTransport(
      new FakeClient({ type: "text", prompt: "t", version: 1, config: { provider_params: [1] } }),
    );
    await expect(badParams.fetchPrompt({ name: "p" })).rejects.toThrow(
      "resolved prompt p@default-label provider_params must be a mapping",
    );
  });

  it("classifies fetch failures into the typed hierarchy", async () => {
    const failing = (error: Error) =>
      new LangfuseRegistryTransport({
        getPrompt: async () => {
          throw error;
        },
      });
    const ref: PromptRef = { name: "missing" };
    await expect(failing(new Error("Prompt not found")).fetchPrompt(ref)).rejects.toBeInstanceOf(
      PromptNotFoundError,
    );
    const outage = new Error("boom");
    outage.name = "LangfuseFetchNetworkError";
    await expect(failing(outage).fetchPrompt(ref)).rejects.toBeInstanceOf(
      PromptRegistryUnavailableError,
    );
    await expect(failing(new Error("Prompt not found")).fetchPrompt(ref)).rejects.toThrow(
      "failed to resolve prompt missing@default-label",
    );
  });

  it("round-trips through registryFromSpec: the #495 gate strips the backend model", async () => {
    const registry = registryFromSpec(specWithRegistry("langfuse"), new LangfuseRegistryTransport(new FakeClient(CHAT_PROMPT)));
    const resolved = await registry!.resolve({ name: "p" });
    // The transport reported model gpt-x; the spec did not allow overrides.
    expect(resolved.model).toBeUndefined();
    expect(resolved.providerParams).toEqual({ top_p: 0.9 });
    expect(resolved.resolvedVersion).toBe("3");
  });
});

describe("langfuseRegistryTransportFromSpec", () => {
  it("returns undefined for every other registry type", async () => {
    await expect(
      langfuseRegistryTransportFromSpec(
        specWithRegistry("inline", "\n    prompts:\n      p: hi"),
        KEYS,
      ),
    ).resolves.toBeUndefined();
    await expect(
      langfuseRegistryTransportFromSpec(specWithRegistry("langsmith"), KEYS),
    ).resolves.toBeUndefined();
  });

  it("THROWS (no observability-style degrade) when the credentials are absent", async () => {
    await expect(langfuseRegistryTransportFromSpec(specWithRegistry("langfuse"), {})).rejects.toThrow(
      /LANGFUSE_PUBLIC_KEY\/LANGFUSE_SECRET_KEY are not set.*registryTransport/s,
    );
  });

  it("builds a transport over the official SDK when the spec + env opt in", async () => {
    // Construct-only: no prompt is fetched, so no network leaves the test.
    const transport = await langfuseRegistryTransportFromSpec(specWithRegistry("langfuse"), {
      ...KEYS,
      LANGFUSE_HOST: "http://localhost:9",
    });
    expect(transport).toBeInstanceOf(LangfuseRegistryTransport);
  });

  it("config errors carry the terminal (non-retryable) contract", async () => {
    const transport = new LangfuseRegistryTransport(
      new FakeClient({ type: "text", prompt: 42 as unknown as string, version: 1 }),
    );
    const registry = new TransportPromptRegistry(transport);
    const error = await registry.resolve({ name: "p" }).catch((raised: unknown) => raised);
    expect(error).toBeInstanceOf(PromptRegistryConfigError);
    expect((error as PromptRegistryConfigError).retryable).toBe(false);
  });
});

describe("transient classification (codex)", () => {
  it("keeps a rate-limited fetch retryable despite the SDK's plain Error", async () => {
    const transport = new LangfuseRegistryTransport({
      async getPrompt() {
        throw new Error("429: rate limit exceeded, too many requests");
      },
    } as never);
    const error = await transport
      .fetchPrompt({ name: "p", label: "production" })
      .then(() => undefined)
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(PromptRegistryUnavailableError);
    expect((error as { retryable: boolean }).retryable).toBe(true);
  });
});
