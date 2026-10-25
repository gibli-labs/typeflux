import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { assembleYamlRuntime, loadYamlSpec, providerTransportsFromSpec } from "../src/index.js";

const sdk = vi.hoisted(() => ({ constructor: vi.fn(), systemOne: vi.fn() }));
vi.mock("@typesafe-ai/sdk", () => ({ TypeSafeClient: class {
  constructor(options: unknown) { sdk.constructor(options); }
  systemOne = sdk.systemOne;
} }));
const yaml = readFileSync(new URL("../examples/typesafe-decisions/typeflux.yaml", import.meta.url), "utf8");
const route = { type: "choice", choice: "billing", confidence: 0.9, probabilities: { billing: 0.95, technical: 0.05, human_review: 0 } };
const schemas = { "schemas:Input": z.object({ text: z.string() }), "schemas:Output": z.object({ route: z.object({ type: z.literal("choice"), choice: z.string(), confidence: z.number(), probabilities: z.object({ billing: z.number(), technical: z.number(), human_review: z.number() }) }) }) };

describe("TypeSafe YAML", () => {
  it("configures the official SDK and executes native decisions", async () => {
    sdk.systemOne.mockResolvedValue({ model: "jev-1.14.0", answers: { route }, usage: { input_tokens: 20, output_tokens: 10 } });
    const spec = loadYamlSpec(yaml, { env: {} });
    const transports = await providerTransportsFromSpec(spec, { TYPESAFE_API_KEY: "fixture-key" });
    expect(sdk.constructor).toHaveBeenLastCalledWith({ apiKey: "fixture-key", baseURL: "https://api.typesafe.ai", retry: { maxRetries: 0 }, logLevel: "off" });
    const runtime = assembleYamlRuntime(spec, { transports: transports!, schemas });
    expect(await runtime.activities["decide"]!({ text: "Refund my duplicate charge" })).toEqual({ route });
    expect(sdk.systemOne.mock.calls.at(-1)?.[0]).toMatchObject({ model: "jev-1.14.0", state: "Refund my duplicate charge" });
    expect(sdk.systemOne.mock.calls.at(-1)?.[1]).toEqual({ timeout: 30_000 });
  });
  it("requires its own key and honors explicit credentials", async () => {
    const spec = loadYamlSpec(yaml.replace("    api_key: {value_from: {env: TYPESAFE_API_KEY}}\n", ""), { env: {} });
    await expect(providerTransportsFromSpec(spec, { OPENAI_API_KEY: "unrelated" })).rejects.toThrow("TYPESAFE_API_KEY");
    const explicit = loadYamlSpec(yaml.replace("api_key: {value_from: {env: TYPESAFE_API_KEY}}", "api_key: explicit-fixture-key"), { env: {} });
    await providerTransportsFromSpec(explicit, { TYPESAFE_API_KEY: "default-key" });
    expect(sdk.constructor.mock.calls.at(-1)?.[0]).toHaveProperty("apiKey", "explicit-fixture-key");
  });
  it("rejects unsupported generation parameters at load", () => {
    expect(() => loadYamlSpec(yaml.replace("timeout: 30", "temperature: 0"), { env: {} })).toThrow("temperature");
  });
});
