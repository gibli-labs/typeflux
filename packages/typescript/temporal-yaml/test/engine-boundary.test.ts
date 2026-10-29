import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { readFileSync } from "node:fs";
import { assembleYamlRuntime, autoWireRuntimeOptions, buildRuntime, typefluxYamlSpec, loadProfileSpec, loadYamlSpec, profileOverridesAndProvenance, validateLocalCapabilities } from "../src/index.js";

const base = JSON.parse(readFileSync(new URL("../../../../contracts/local-runtime/golden/capability-cases.json", import.meta.url), "utf8")).base;

describe("Temporal engine boundary", () => {
  it("rejects local root and children before runtime services", async () => {
    const provider = vi.fn(() => { throw new Error("provider construction reached"); });
    const options = { schemas: {"schemas:Value": z.object({text: z.string()})}, get provider(): never { return provider(); } };
    const local = typefluxYamlSpec.parse(base);
    expect(() => assembleYamlRuntime(local, options)).toThrow(/Temporal entry point/);
    await expect(autoWireRuntimeOptions(local, options)).rejects.toThrow(/Temporal entry point/);
    await expect(buildRuntime(local, options)).rejects.toThrow(/Temporal entry point/);
    const raw = structuredClone(base);
    raw.runtime.engine = "temporal";
    raw.task_queue = "worker";
    raw.workflow.steps = [{id: "nested", workflow: "child", when: {path: "input.text", eq: "never"}}];
    const parent = typefluxYamlSpec.parse(raw);
    const composed = Object.create(options);
    composed.subworkflows = {selfId: "parent", specFor: () => local};
    expect(() => assembleYamlRuntime(parent, composed)).toThrow(/Temporal entry point/);
    await expect(autoWireRuntimeOptions(parent, composed)).rejects.toThrow(/Temporal entry point/);
    expect(provider).not.toHaveBeenCalled();
  });
});


it("selects engine from a runtime component profile before schema validation", () => {
  const profile = loadProfileSpec("name: local\nkind: runtime\nruntime: {engine: local}\n");
  const raw = structuredClone(base);
  delete raw.runtime.engine;
  const {overrides} = profileOverridesAndProvenance([{id: "local", spec: profile, sourcePath: "profile.yaml"}]);
  const local = loadYamlSpec(JSON.stringify(raw), {overrides});
  expect(validateLocalCapabilities(local)).toEqual(["root"]);
});
