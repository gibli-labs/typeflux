import { describe, expect, it } from "vitest";

import { assertSafeKeys, deepMerge, loadYamlSpec } from "../src/index.js";

/** A minimal workflow spec with a base task_queue + provider, overridable via options. */
const WORKFLOW = `
project: p
name: n
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s
      activity: a
`;

describe("loadYamlSpec overrides + runtimeDefaults (#454; Python load_yaml_spec)", () => {
  it("overrides win over the workflow YAML (task_queue + a deep runtime merge)", () => {
    const spec = loadYamlSpec(WORKFLOW, {
      overrides: { task_queue: "prod-queue", runtime: { temporal: { namespace: "prod" } } },
    });
    expect(spec.task_queue).toBe("prod-queue");
    // Deep merge: the override's temporal.namespace is added; the base provider is untouched.
    expect(spec.runtime.temporal.namespace).toBe("prod");
    expect(spec.runtime.provider.type).toBe("openai");
  });

  it("a partial runtime override deep-merges (keeps sibling keys from the YAML)", () => {
    const spec = loadYamlSpec(WORKFLOW, { overrides: { runtime: { provider: { type: "anthropic" } } } });
    expect(spec.runtime.provider.type).toBe("anthropic");
    expect(spec.runtime.provider.model).toBe("gpt-4o-mini"); // sibling kept from the base YAML
  });

  it("runtimeDefaults sit BENEATH the YAML (the YAML wins) but fill unset blocks", () => {
    const spec = loadYamlSpec(WORKFLOW, {
      runtimeDefaults: { provider: { type: "anthropic" }, provider_retry: { max_attempts: 3 } },
    });
    // The YAML sets provider.type → it wins over the default.
    expect(spec.runtime.provider.type).toBe("openai");
    // The YAML omits provider_retry → the default fills it.
    expect(spec.runtime.provider_retry?.max_attempts).toBe(3);
  });

  it("defaults < YAML < overrides precedence holds across all three layers", () => {
    const spec = loadYamlSpec(WORKFLOW, {
      runtimeDefaults: { provider: { type: "anthropic" } },
      overrides: { runtime: { provider: { type: "gemini" } } },
    });
    expect(spec.runtime.provider.type).toBe("gemini"); // override beats both YAML and default
  });

  it("overrides are applied BEFORE interpolation (an override value may reference ${VAR})", () => {
    const spec = loadYamlSpec(WORKFLOW, {
      overrides: { runtime: { temporal: { address: "${OVERRIDE_ADDR}" } } },
      env: { OVERRIDE_ADDR: "prod.temporal:7233" },
    });
    expect(spec.runtime.temporal.address).toBe("prod.temporal:7233");
  });

  it("enforces the override allow-list (top-level and runtime.*)", () => {
    expect(() => loadYamlSpec(WORKFLOW, { overrides: { activities: {} } })).toThrow(
      /activities is not an allowed environment override/,
    );
    expect(() => loadYamlSpec(WORKFLOW, { overrides: { runtime: { activity_retry: {} } } })).toThrow(
      /runtime.activity_retry is not an allowed runtime override/,
    );
    // runtimeDefaults are allow-listed too (they become a `{ runtime: … }` layer).
    expect(() => loadYamlSpec(WORKFLOW, { runtimeDefaults: { activity_retry: {} } })).toThrow(
      /runtime.activity_retry is not an allowed runtime override/,
    );
  });

  it("an empty overrides / runtimeDefaults is a no-op", () => {
    const spec = loadYamlSpec(WORKFLOW, { overrides: {}, runtimeDefaults: {} });
    expect(spec.task_queue).toBe("base-queue");
  });

  it("a null overrides / runtimeDefaults no-ops (a caller's None-equivalent — no TypeError)", () => {
    const nullish = null as unknown as Record<string, unknown>;
    const spec = loadYamlSpec(WORKFLOW, { overrides: nullish, runtimeDefaults: nullish });
    expect(spec.task_queue).toBe("base-queue");
  });
});

describe("prototype-pollution defense (#454; codex)", () => {
  const WITH_PROTO = (body: string) => `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider:
    type: openai
${body}
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

  it("rejects a __proto__ key IN THE YAML (the yaml lib materializes it; zod's strict ignores it)", () => {
    // Guard: no global pollution AND the load is rejected (not silently accepted).
    expect(() => loadYamlSpec(WITH_PROTO("    __proto__:\n      polluted: true"))).toThrow(/unsafe key '__proto__'/);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  it("rejects a nested __proto__ introduced via an override (own key, not the prototype setter)", () => {
    const evil = JSON.parse('{"runtime":{"provider":{"__proto__":{"x":1}}}}') as Record<string, unknown>;
    expect(() => loadYamlSpec(WORKFLOW, { overrides: evil })).toThrow(/unsafe key '__proto__'/);
    expect(({} as Record<string, unknown>).x).toBeUndefined();
  });

  it("deepMerge never routes a dangerous key through the prototype setter (no pollution)", () => {
    const evil = JSON.parse('{"__proto__": {"polluted": true}}') as Record<string, unknown>;
    const merged = deepMerge({ a: 1 }, evil);
    expect(Object.hasOwn(merged, "__proto__")).toBe(true); // preserved as an OWN key, not the prototype
    expect(({} as Record<string, unknown>).polluted).toBeUndefined(); // Object.prototype untouched
    expect(Object.getPrototypeOf(merged)).toBe(Object.prototype); // merged's own prototype untouched
  });

  it("assertSafeKeys rejects __proto__ only; constructor/prototype are legitimate open-dict keys", () => {
    // JSON.parse materializes an OWN __proto__ (an object literal would set the prototype).
    expect(() => assertSafeKeys(JSON.parse('{"runtime":{"provider":{"__proto__":{"x":1}}}}'), "x")).toThrow(
      /unsafe key '__proto__'/,
    );
    // constructor/prototype are valid map keys (a provider/workflow/prompt so named) that
    // Python's dict accepts and zod's strict schema rejects on its own where unknown.
    expect(() => assertSafeKeys({ providers: { allowed: { constructor: {} } } }, "x")).not.toThrow();
    expect(() => assertSafeKeys([{ prototype: 1 }], "x")).not.toThrow();
    expect(() => assertSafeKeys({ a: { b: [1, 2, { c: 3 }] } }, "x")).not.toThrow();
  });
});

describe("deepMerge (#454; Python _deep_merge)", () => {
  it("recursively merges mappings; right wins per key", () => {
    expect(deepMerge({ a: { x: 1, y: 2 }, b: 1 }, { a: { y: 3, z: 4 }, c: 5 })).toEqual({
      a: { x: 1, y: 3, z: 4 },
      b: 1,
      c: 5,
    });
  });

  it("replaces arrays and scalars wholesale (not element-merged) and does not mutate inputs", () => {
    const left = { a: [1, 2], b: { x: 1 } };
    const right = { a: [3], b: 9 };
    expect(deepMerge(left, right)).toEqual({ a: [3], b: 9 });
    expect(left).toEqual({ a: [1, 2], b: { x: 1 } }); // unmutated
  });

  it("a mapping replacing a scalar (and vice versa) takes the right value", () => {
    expect(deepMerge({ a: 1 }, { a: { x: 1 } })).toEqual({ a: { x: 1 } });
    expect(deepMerge({ a: { x: 1 } }, { a: 1 })).toEqual({ a: 1 });
  });
});
