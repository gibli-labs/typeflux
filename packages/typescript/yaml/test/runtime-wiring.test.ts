import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { typefluxYamlSpec } from "../src/spec.js";
import { composeProjectPolicies } from "../src/policy-composition.js";
import { loadPolicySpec } from "../src/policy.js";
import { autoWireRuntimeOptions } from "../src/runtime-wiring.js";

const factories = vi.hoisted(() => ({observer: vi.fn(), registry: vi.fn(), transports: vi.fn()}));
vi.mock("../src/langfuse-observer.js", () => ({langfuseObserverFromSpec: factories.observer}));
vi.mock("../src/langfuse-registry.js", () => ({langfuseRegistryTransportFromSpec: factories.registry}));
vi.mock("../src/provider-transports-from-env.js", () => ({providerTransportsFromSpec: factories.transports}));
const base = JSON.parse(readFileSync(new URL("../../../../contracts/local-runtime/golden/execution-cases.json", import.meta.url), "utf8")).base;
beforeEach(() => { for (const factory of Object.values(factories)) factory.mockReset().mockResolvedValue(undefined); });

describe("shared automatic runtime wiring", () => {
  it("refuses skipped local capabilities and project policy before any automatic clients", async () => {
    const root = typefluxYamlSpec.parse({...base, workflow: {...base.workflow, steps: [{id: "skip", workflow: "child", when: {path: "input.n", eq: -1}}]}});
    const child = typefluxYamlSpec.parse({...base, name: "child", workflow: {...base.workflow, risk_tier: "human_gated"}});
    await expect(autoWireRuntimeOptions(root, {schemas: {}, subworkflows: {specFor: () => child}})).rejects.toThrow(/engine: temporal/);
    const policy = composeProjectPolicies([{id: "strict", spec: loadPolicySpec("name: strict\nproviders: {allowed: {openai: {models: [m]}}}\n")}], ["strict"]);
    await expect(autoWireRuntimeOptions(typefluxYamlSpec.parse(base), {schemas: {}, policy})).rejects.toThrow(/policy/);
    for (const factory of Object.values(factories)) expect(factory).not.toHaveBeenCalled();
  });

  it("shuts down an automatic observer if subsequent service construction fails", async () => {
    const shutdown = vi.fn().mockResolvedValue(undefined);
    factories.observer.mockResolvedValue({shutdown});
    factories.transports.mockRejectedValue(new Error("provider construction failed"));
    await expect(autoWireRuntimeOptions(typefluxYamlSpec.parse(base), {schemas: {}})).rejects.toThrow("provider construction failed");
    expect(shutdown).toHaveBeenCalledOnce();
  });

  it("returns the owned observer for foreground shutdown without closing it prematurely", async () => {
    const shutdown = vi.fn().mockResolvedValue(undefined);
    const observer = {shutdown};
    factories.observer.mockResolvedValue(observer);
    const result = await autoWireRuntimeOptions(typefluxYamlSpec.parse(base), {schemas: {}});
    expect(result.autoObserver).toBe(observer); expect(result.options).toMatchObject({observer});
    expect(shutdown).not.toHaveBeenCalled();
    await result.autoObserver!.shutdown(); expect(shutdown).toHaveBeenCalledOnce();
  });
});
