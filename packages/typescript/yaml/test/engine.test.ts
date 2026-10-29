import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { loadYamlSpec, requireTemporalEngine, runtimeEngine, RuntimeCapabilityError,
  typefluxYamlSpec, validateLocalCapabilities, type TypefluxYamlSpec } from "../src/index.js";

const fixtures = JSON.parse(readFileSync(new URL("../../../../contracts/local-runtime/golden/capability-cases.json", import.meta.url), "utf8"));
function localSpec(name = "root", steps?: unknown[]): TypefluxYamlSpec {
  const raw = structuredClone(fixtures.base);
  raw.name = raw.workflow.name = name;
  if (steps !== undefined) raw.workflow.steps = steps;
  return typefluxYamlSpec.parse(raw);
}

describe("engine boundaries", () => {
  it("matches shared local capability cases", () => {
    for (const testCase of fixtures.cases) {
      const raw = structuredClone(fixtures.base);
      for (const patch of testCase.patches) {
        let current = raw;
        for (const key of patch.path.slice(0, -1)) current = current[key];
        current[patch.path.at(-1)] = patch.value;
      }
      const spec = typefluxYamlSpec.parse(raw);
      if (testCase.errorPath !== undefined) {
        let caught: unknown;
        try { validateLocalCapabilities(spec); } catch (error) { caught = error; }
        expect(caught, testCase.name).toBeInstanceOf(RuntimeCapabilityError);
        expect(caught).toMatchObject({path: testCase.errorPath, workflow: "Root"});
        expect(String(caught)).toContain("runtime.engine: temporal");
      } else expect(validateLocalCapabilities(spec), testCase.name).toEqual(["root"]);
    }
  });

  it("checks skipped nested maps and references", () => {
    const root = localSpec("root", [{id: "nested", workflow: "child", when: {path: "input.text", eq: "never"}}]);
    const child = localSpec("child", [{id: "items", map: {workflow: "leaf", over: "input.items", concurrency: 2, collect: {output: "schemas:List", field: "items"}}}]);
    const leaf = localSpec("leaf");
    leaf.workflow.risk_tier = "human_gated";
    const lookup = new Map([["child", child], ["leaf", leaf]]);
    const resolver = {specFor: (id: string) => lookup.get(id)};
    expect(() => validateLocalCapabilities(root, resolver)).toThrow(/leaf.*workflow.risk_tier/);
    delete leaf.workflow.risk_tier;
    expect(validateLocalCapabilities(root, resolver)).toEqual(["root", "child", "leaf"]);
    expect(() => validateLocalCapabilities(root)).toThrow(/project resolver/);
    expect(() => validateLocalCapabilities(root, {specFor: () => undefined})).toThrow(/not declared/);
    leaf.project = "different";
    expect(() => validateLocalCapabilities(root, resolver)).toThrow(/cross-project/);
    leaf.project = root.project;
    lookup.set("leaf", root);
    expect(() => validateLocalCapabilities(root, resolver)).toThrow(/cyclic/);
    for (const engine of [undefined, "temporal"] as const) {
      lookup.set("child", typefluxYamlSpec.parse({
        ...child, runtime: {...child.runtime, engine}, task_queue: "existing",
      }));
      let caught: unknown;
      try { validateLocalCapabilities(root, resolver); } catch (error) { caught = error; }
      expect(caught).toBeInstanceOf(RuntimeCapabilityError);
      expect(caught).toMatchObject({workflow: "child", path: "runtime.engine"});
      expect(String(caught)).toContain("local graph requires");
      expect(String(caught)).toContain("Use runtime.engine: local");
      expect(String(caught)).not.toContain("Use runtime.engine: temporal");
    }
  });

  it("resolves diamond children once and preserves resolver errors", () => {
    const root = localSpec("root", [{id: "one", workflow: "a"}, {id: "two", workflow: "b"}]);
    const lookup = new Map(["a", "b"].map((name) => [name, localSpec(name, [{id: "shared", workflow: "c"}])]));
    lookup.set("c", localSpec("c"));
    const calls: string[] = [];
    const resolver = {specFor: (id: string) => { calls.push(id); return lookup.get(id); }};
    expect(validateLocalCapabilities(root, resolver)).toEqual(["root", "a", "c", "b"]);
    expect(calls).toEqual(["a", "c", "b"]);
    const error = new Error("invalid child environment");
    expect(() => validateLocalCapabilities(root, {specFor: () => { throw error; }})).toThrow(error);
    const aliasRoot = localSpec("alias", [{id: "nested", workflow: "alias"}]);
    expect(validateLocalCapabilities(aliasRoot, {specFor: () => localSpec("different")})).toEqual(["alias", "alias"]);
  });

  it("preserves defaults and resolves engine overrides before validation", () => {
    const raw = structuredClone(fixtures.base);
    delete raw.runtime.engine;
    raw.task_queue = "existing";
    const temporal = typefluxYamlSpec.parse(raw);
    expect(runtimeEngine(temporal)).toBe("temporal");
    expect(requireTemporalEngine(temporal)).toBe("existing");
    expect(temporal.runtime).not.toHaveProperty("engine");
    delete raw.task_queue;
    expect(() => typefluxYamlSpec.parse(raw)).toThrow(/task_queue/);
    const local = loadYamlSpec(JSON.stringify(raw), {runtimeDefaults: {engine: "temporal"}, overrides: {runtime: {engine: "local"}}});
    expect(runtimeEngine(local)).toBe("local");
    expect(validateLocalCapabilities(local)).toEqual(["root"]);
    expect(validateLocalCapabilities(typefluxYamlSpec.parse(JSON.parse(JSON.stringify(local))))).toEqual(["root"]);
    for (const engine of ["unknown", null]) {
      raw.runtime.engine = engine;
      expect(() => typefluxYamlSpec.parse(raw)).toThrow(/engine/);
    }
  });
});
