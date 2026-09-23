/**
 * Out-of-the-box vendor providers (`provider-transports-from-env.ts`): the
 * spec/env gate `buildRuntime` calls (type gating, spec `api_key` precedence,
 * missing-key throws, gemini's GEMINI/GOOGLE fallback), the Python
 * `resolve_optional_secret_text` rules for `runtime.provider.api_key`, and the
 * three thin SDK adapters — each faked structurally, then round-tripped
 * through `providerFromSpec` so the full spec → provider → vendor-SDK path is
 * exercised offline. The install-hint path (SDK peer absent) is untestable
 * here — the vendors are devDependencies of this package — and is covered by
 * the error text mirroring the langfuse/langsmith modules. Env is always
 * passed EXPLICITLY: the shared network guard scrubs the OPENAI_, ANTHROPIC_,
 * and GEMINI_ prefixes plus GOOGLE_API_KEY from unit-test env.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import type {
  AnthropicMessagesRequest,
  AnthropicMessagesResponse,
  ChatCompletionRequest,
  ChatCompletionResponse,
  GeminiFile,
  GeminiGenerateContentResponse,
} from "@typeflux/temporal";

import {
  anthropicTransportFromSdk,
  geminiTransportFromSdk,
  loadYamlSpec,
  openaiTransportFromSdk,
  providerFromSpec,
  providerTransportsFromSpec,
  resolveProviderApiKey,
} from "../src/index.js";

const YAML = `
project: prov_demo
name: prov_demo
task_queue: prov-demo
runtime:
  temporal:
    address: localhost:7233
  registry:
    type: inline
    prompts:
      p: hi
  provider:
    type: openai
activities:
  definitions:
    - name: act
      input: schemas:Item
      output: schemas:Item
      prompt: p
workflow:
  name: ProvDemoWorkflow
  input: schemas:Item
  output: schemas:Item
  steps:
    - id: act
      activity: act
`;

function specWithProvider(type: string, extra = "") {
  return loadYamlSpec(YAML.replace("    type: openai", `    type: ${type}${extra}`));
}

describe("resolveProviderApiKey", () => {
  it("passes a literal through untouched; an empty literal is absent (Python rule)", () => {
    expect(resolveProviderApiKey(specWithProvider("openai", '\n    api_key: " sk-lit "'), {})).toBe(" sk-lit ");
    expect(resolveProviderApiKey(specWithProvider("openai", '\n    api_key: ""'), {})).toBeUndefined();
    expect(resolveProviderApiKey(specWithProvider("openai"), {})).toBeUndefined();
  });

  it("resolves value_from.env with trim, and throws Python's texts when required", () => {
    const spec = specWithProvider("openai", "\n    api_key: { value_from: { env: MY_KEY } }");
    expect(resolveProviderApiKey(spec, { MY_KEY: " sk-env \n" })).toBe("sk-env");
    expect(() => resolveProviderApiKey(spec, {})).toThrow(
      "missing required secret for runtime.provider.api_key: env MY_KEY is not set",
    );
    expect(() => resolveProviderApiKey(spec, { MY_KEY: "  " })).toThrow(
      "secret for runtime.provider.api_key from env MY_KEY is empty",
    );
    // `required: false` degrades an unset/empty reference to absent, not a throw.
    const optional = specWithProvider(
      "openai",
      "\n    api_key: { value_from: { env: MY_KEY, required: false } }",
    );
    expect(resolveProviderApiKey(optional, {})).toBeUndefined();
    expect(resolveProviderApiKey(optional, { MY_KEY: "" })).toBeUndefined();
  });

  const dir = mkdtempSync(join(tmpdir(), "typeflux-secret-"));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));

  it("resolves value_from.file with trim, and throws Python's texts when required", () => {
    const keyFile = join(dir, "key.txt");
    writeFileSync(keyFile, " sk-file \n");
    const spec = (file: string, extra = "") =>
      specWithProvider("openai", `\n    api_key: { value_from: { file: "${file}"${extra} } }`);
    expect(resolveProviderApiKey(spec(keyFile), {})).toBe("sk-file");
    expect(() => resolveProviderApiKey(spec(join(dir, "absent.txt")), {})).toThrow(
      `missing required secret for runtime.provider.api_key: file ${join(dir, "absent.txt")} does not exist`,
    );
    const emptyFile = join(dir, "empty.txt");
    writeFileSync(emptyFile, "  \n");
    expect(() => resolveProviderApiKey(spec(emptyFile), {})).toThrow(
      `secret for runtime.provider.api_key from file ${emptyFile} is empty`,
    );
    expect(resolveProviderApiKey(spec(join(dir, "absent.txt"), ", required: false"), {})).toBeUndefined();
  });
});

describe("providerTransportsFromSpec", () => {
  it("returns undefined for every non-vendor type (the injected-provider seam)", async () => {
    await expect(providerTransportsFromSpec(specWithProvider("acme-custom"), {})).resolves.toBeUndefined();
  });

  it("THROWS (no observability-style degrade) when the vendor key is absent", async () => {
    await expect(providerTransportsFromSpec(specWithProvider("openai"), {})).rejects.toThrow(
      /OPENAI_API_KEY is not set.*cannot degrade past its provider.*`provider`\/`transports`/s,
    );
    await expect(providerTransportsFromSpec(specWithProvider("anthropic"), {})).rejects.toThrow(
      /ANTHROPIC_API_KEY is not set/,
    );
    await expect(providerTransportsFromSpec(specWithProvider("gemini"), {})).rejects.toThrow(
      /GEMINI_API_KEY\/GOOGLE_API_KEY is not set/,
    );
    // Truthy `||`: an interpolated empty value is no credential.
    await expect(providerTransportsFromSpec(specWithProvider("openai"), { OPENAI_API_KEY: "" })).rejects.toThrow(
      /OPENAI_API_KEY is not set/,
    );
  });

  it("builds each vendor's transport over the official SDK when the spec + env opt in", async () => {
    // Construct-only: no call is made, so no network leaves the test.
    await expect(
      providerTransportsFromSpec(specWithProvider("openai"), { OPENAI_API_KEY: "sk-o" }),
    ).resolves.toHaveProperty("openai");
    await expect(
      providerTransportsFromSpec(specWithProvider("anthropic"), {
        ANTHROPIC_API_KEY: "sk-a",
        ANTHROPIC_BASE_URL: "http://localhost:9",
      }),
    ).resolves.toHaveProperty("anthropic");
    await expect(
      providerTransportsFromSpec(specWithProvider("gemini"), { GEMINI_API_KEY: "sk-g" }),
    ).resolves.toHaveProperty("gemini");
    // Python `GEMINI_API_KEY or GOOGLE_API_KEY`: the fallback engages, incl. past
    // an interpolated empty GEMINI_API_KEY (truthy `||`, not `??`).
    await expect(
      providerTransportsFromSpec(specWithProvider("gemini"), { GEMINI_API_KEY: "", GOOGLE_API_KEY: "sk-gg" }),
    ).resolves.toHaveProperty("gemini");
  });

  it("prefers the spec's api_key slot over the vendor's standard variable", async () => {
    // No OPENAI_API_KEY in env — the spec slot alone must satisfy the gate.
    const spec = specWithProvider("openai", "\n    api_key: { value_from: { env: MY_KEY } }");
    await expect(providerTransportsFromSpec(spec, { MY_KEY: "sk-spec" })).resolves.toHaveProperty("openai");
    // A required-but-unset slot throws BEFORE the default variable is consulted
    // (a declared credential slot must not silently fall through).
    await expect(providerTransportsFromSpec(spec, { OPENAI_API_KEY: "sk-o" })).rejects.toThrow(
      "missing required secret for runtime.provider.api_key: env MY_KEY is not set",
    );
  });
});

describe("openaiTransportFromSdk", () => {
  it("forwards the request and maps signal/timeoutMs onto the SDK's request options", async () => {
    const calls: { request: ChatCompletionRequest; options: unknown }[] = [];
    const response: ChatCompletionResponse = {
      choices: [{ message: { content: '{"ok": true}' }, finish_reason: "stop" }],
      usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
    };
    const transport = openaiTransportFromSdk({
      chat: {
        completions: {
          create: async (request, options) => {
            calls.push({ request, options });
            return response;
          },
        },
      },
    });
    const request: ChatCompletionRequest = {
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
      response_format: { type: "json_schema", json_schema: { name: "response", schema: {}, strict: true } },
    };
    const controller = new AbortController();
    await expect(
      transport.chat.completions.create(request, { signal: controller.signal, timeoutMs: 1500 }),
    ).resolves.toBe(response);
    expect(calls).toEqual([{ request, options: { signal: controller.signal, timeout: 1500 } }]);
    // No call options → none materialized for the SDK (its own defaults apply).
    await transport.chat.completions.create(request);
    expect(calls[1]?.options).toBeUndefined();
  });
});

describe("anthropicTransportFromSdk", () => {
  it("forwards the request and maps signal/timeoutMs onto the SDK's request options", async () => {
    const calls: { request: AnthropicMessagesRequest; options: unknown }[] = [];
    const response: AnthropicMessagesResponse = {
      content: [{ type: "text", text: '{"ok": true}' }],
      stop_reason: "end_turn",
      usage: { input_tokens: 3, output_tokens: 2 },
    };
    const transport = anthropicTransportFromSdk({
      messages: {
        create: async (request, options) => {
          calls.push({ request, options });
          return response;
        },
      },
    });
    const request: AnthropicMessagesRequest = {
      model: "claude-sonnet-4-6",
      max_tokens: 4096,
      messages: [{ role: "user", content: "hi" }],
      output_config: { format: { type: "json_schema", schema: {} } },
    };
    const controller = new AbortController();
    await expect(transport.messages.create(request, { signal: controller.signal, timeoutMs: 900 })).resolves.toBe(
      response,
    );
    expect(calls).toEqual([{ request, options: { signal: controller.signal, timeout: 900 } }]);
    await transport.messages.create(request);
    expect(calls[1]?.options).toBeUndefined();
  });
});

describe("geminiTransportFromSdk", () => {
  it("flattens the request into the SDK's single config and wires files + caches", async () => {
    const generateCalls: unknown[] = [];
    const response: GeminiGenerateContentResponse = {
      candidates: [{ content: { parts: [{ text: '{"ok": true}' }] }, finishReason: "STOP" }],
      usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2, totalTokenCount: 5 },
    };
    const uploadedFile: GeminiFile = { name: "files/x", uri: "gs://x", state: "ACTIVE" };
    const cacheCalls: unknown[] = [];
    const fileCalls: unknown[] = [];
    const transport = geminiTransportFromSdk({
      models: {
        generateContent: async (params) => {
          generateCalls.push(params);
          return response;
        },
      },
      files: {
        upload: async (params) => {
          fileCalls.push(["upload", params]);
          return uploadedFile;
        },
        get: async (params) => {
          fileCalls.push(["get", params]);
          return uploadedFile;
        },
      },
      caches: {
        create: async (params) => {
          cacheCalls.push(["create", params]);
          return { name: "caches/y" };
        },
        delete: async (params) => {
          cacheCalls.push(["delete", params]);
          return {};
        },
      },
    });
    const controller = new AbortController();
    await expect(
      transport.generateContent(
        {
          model: "gemini-2.5-flash",
          contents: [{ role: "user", parts: [{ text: "hi" }] }],
          systemInstruction: { parts: [{ text: "sys" }] },
          generationConfig: { responseMimeType: "application/json", responseSchema: {}, temperature: 0.1 },
        },
        { signal: controller.signal, timeoutMs: 2000 },
      ),
    ).resolves.toBe(response);
    expect(generateCalls).toEqual([
      {
        model: "gemini-2.5-flash",
        contents: [{ role: "user", parts: [{ text: "hi" }] }],
        config: {
          systemInstruction: { parts: [{ text: "sys" }] },
          responseMimeType: "application/json",
          responseSchema: {},
          temperature: 0.1,
          abortSignal: controller.signal,
          httpOptions: { timeout: 2000 },
        },
      },
    ]);
    // cachedContent must be FORWARDED (or session-cached calls silently lose
    // their prefix); the provider already omitted systemInstruction for it.
    await transport.generateContent({
      model: "gemini-2.5-flash",
      contents: [{ role: "user", parts: [{ text: "item" }] }],
      cachedContent: "caches/y",
      generationConfig: { responseMimeType: "application/json", responseSchema: {} },
    });
    expect(generateCalls[1]).toEqual({
      model: "gemini-2.5-flash",
      contents: [{ role: "user", parts: [{ text: "item" }] }],
      config: {
        cachedContent: "caches/y",
        responseMimeType: "application/json",
        responseSchema: {},
      },
    });
    // The optional surfaces pass through: Files API (#503) and cachedContent (#478).
    await transport.files?.upload({ file: "/tmp/big.mp4", config: { mimeType: "video/mp4" } });
    await transport.files?.get({ name: "files/x" });
    await transport.caches?.create({ model: "gemini-2.5-flash", config: { contents: [] } });
    await transport.caches?.delete({ name: "caches/y" });
    expect(fileCalls).toEqual([
      ["upload", { file: "/tmp/big.mp4", config: { mimeType: "video/mp4" } }],
      ["get", { name: "files/x" }],
    ]);
    expect(cacheCalls).toEqual([
      ["create", { model: "gemini-2.5-flash", config: { contents: [] } }],
      ["delete", { name: "caches/y" }],
    ]);
  });
});

describe("providerFromSpec round-trip", () => {
  it("runs a structured call through the spec-selected provider over a faked SDK", async () => {
    const requests: ChatCompletionRequest[] = [];
    const transports = {
      openai: openaiTransportFromSdk({
        chat: {
          completions: {
            create: async (request) => {
              requests.push(request);
              return { choices: [{ message: { content: '{"summary": "ok"}' }, finish_reason: "stop" }] };
            },
          },
        },
      }),
    };
    const provider = providerFromSpec(specWithProvider("openai"), transports);
    const result = await provider.structuredCall({
      messages: [{ role: "user", content: "summarize" }],
      outputSchema: { type: "object" },
    });
    expect(result).toEqual({ summary: "ok" });
    // The spec omitted `model`, so the type's default rode the wire (Python
    // `provider.model or DEFAULT_OPENAI_MODEL`).
    expect(requests[0]?.model).toBe("gpt-4o-mini");
  });
});

describe("real SDK clients are assignable to the adapter helpers (codex)", () => {
  it("type-checks the official clients against the structural interfaces", async () => {
    // Compile-time proof: constructing each official client and passing it to
    // the exported helper must type-check WITHOUT casts (`never`-param
    // contravariance). Construct-only — no network leaves the test.
    const { default: OpenAI } = await import("openai");
    const { default: Anthropic } = await import("@anthropic-ai/sdk");
    const { GoogleGenAI } = await import("@google/genai");
    expect(openaiTransportFromSdk(new OpenAI({ apiKey: "test" }))).toBeDefined();
    expect(anthropicTransportFromSdk(new Anthropic({ apiKey: "test" }))).toBeDefined();
    expect(geminiTransportFromSdk(new GoogleGenAI({ apiKey: "test" }))).toBeDefined();
  });
});

describe("finder regressions", () => {
  it("stays inert for non-vendor types even with a broken api_key slot", async () => {
    // The injected-provider seam: a custom provider's misconfigured api_key
    // must surface assembleYamlRuntime's clear provider/transports error, not
    // a secret-resolution throw from the vendor-only auto path (finder).
    const spec = specWithProvider(
      "custom",
      "\n    api_key:\n      value_from:\n        env: DEFINITELY_UNSET_VAR",
    );
    await expect(providerTransportsFromSpec(spec, {})).resolves.toBeUndefined();
  });

  it("the injected environment is authoritative for base URLs", async () => {
    // Omitting baseURL would make the SDK read process.env directly; a scoped
    // map without the var must pin the vendor default instead (finder).
    const transports = await providerTransportsFromSpec(specWithProvider("openai"), {
      OPENAI_API_KEY: "sk-test",
    });
    expect(transports).toBeDefined();
    // Constructing again with an explicit base URL uses it (construct-only).
    const custom = await providerTransportsFromSpec(specWithProvider("openai"), {
      OPENAI_API_KEY: "sk-test",
      OPENAI_BASE_URL: "http://localhost:9/v1",
    });
    expect(custom).toBeDefined();
  });
});
