import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { assembleYamlRuntime, loadYamlSpec, providerTransportsFromSpec } from "../src/index.js";

const sdk = vi.hoisted(() => ({ constructor: vi.fn(), create: vi.fn() }));
vi.mock("openai", () => ({ default: class {
  constructor(options: unknown) { sdk.constructor(options); }
  chat = { completions: { create: sdk.create } };
} }));
const yaml = readFileSync(new URL("../examples/vercel-ai-gateway/typeflux.yaml", import.meta.url), "utf8");
const schemas = { "schemas:Input": z.object({ text: z.string() }), "schemas:Output": z.object({ label: z.string() }) };

describe("Vercel AI Gateway YAML", () => {
  it("configures the gateway SDK and executes the example activity", async () => {
    sdk.create.mockResolvedValue({ choices: [{ message: { content: '{"label":"billing"}' } }] });
    const spec = loadYamlSpec(yaml, { env: {} });
    const transports = await providerTransportsFromSpec(spec, { AI_GATEWAY_API_KEY: "fixture-key", OPENAI_BASE_URL: "https://wrong.invalid" });
    expect(sdk.constructor).toHaveBeenLastCalledWith({ apiKey: "fixture-key", baseURL: "https://ai-gateway.vercel.sh/v1", maxRetries: 0 });
    const runtime = assembleYamlRuntime(spec, { transports: transports!, schemas });
    expect(await runtime.activities["classify"]!({ text: "invoice" })).toEqual({ label: "billing" });
    expect(sdk.create.mock.calls.at(-1)?.[0]).toMatchObject({ model: "openai/gpt-4o-mini", max_tokens: 128 });
    expect(sdk.create.mock.calls.at(-1)?.[1]).toMatchObject({ timeout: 30_000 });
  });

  it("requires a gateway key without falling back to OpenAI credentials", async () => {
    const spec = loadYamlSpec(yaml.replace("    api_key: {value_from: {env: AI_GATEWAY_API_KEY}}\n", ""), { env: {} });
    await expect(providerTransportsFromSpec(spec, { OPENAI_API_KEY: "unrelated" })).rejects.toThrow("AI_GATEWAY_API_KEY");
  });

  it("rejects missing models and unsupported gateway options during load", () => {
    expect(() => loadYamlSpec(yaml.replace("    model: openai/gpt-4o-mini\n", ""), { env: {} })).toThrow("author/model");
    expect(() => loadYamlSpec(yaml.replace("max_tokens: 128", "seed: 1"), { env: {} })).toThrow("seed");
    expect(() => loadYamlSpec(yaml.replace("max_tokens: 128", "providerOptions: {}"), { env: {} })).toThrow();
  });
});
