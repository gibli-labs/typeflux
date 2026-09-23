import { describe, expect, it } from "vitest";

import {
  composeProjectPolicies,
  loadPolicySpec,
  loadYamlSpec,
  validatePolicyCompliance,
  type ComposedProjectPolicy,
  type TypefluxYamlSpec,
} from "../src/index.js";

/**
 * Composition ceilings policy dimension (#298 Phase A) — TS edition. Mirrors the
 * Python `test_composition_ceilings`: each knob's reject + pass, the min/AND merge,
 * and the nesting>3 policy-load error. (Closure depth is in project-enforcement.test.)
 */

function spec(steps: string): TypefluxYamlSpec {
  return loadYamlSpec(
    `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  output: schemas:Out
  steps:
${steps}
`,
    { env: {} },
  );
}

/** A composed policy carrying only a composition payload (the validators read `.payload`). */
function comp(composition: Record<string, unknown>): ComposedProjectPolicy {
  return {
    selectedPolicyIds: ["c"],
    appliedPolicyIds: ["c"],
    policyNames: ["c"],
    policyHash: "h",
    payload: { composition },
  };
}

function check(s: TypefluxYamlSpec, composition: Record<string, unknown>) {
  return validatePolicyCompliance(s, comp(composition)).find((c) => c.code === "policy_composition_ceilings")!;
}

const PARALLEL = `    - id: par
      parallel:
        branches:
          - { id: b1, steps: [{ id: b1s, activity: a }] }
          - { id: b2, steps: [{ id: b2s, activity: a }] }
          - { id: b3, steps: [{ id: b3s, activity: a }] }
        collect: { output: schemas:Out }`;

const MAP_WF = `    - id: m
      map: { workflow: child, over: input.items, concurrency: 2, collect: { output: schemas:Out, field: result } }`;

describe("validateComposition (#298; Python _validate_composition)", () => {
  it("skips when the policy declares no composition", () => {
    expect(check(spec("    - { id: s, activity: a }"), {}).status).toBe("skipped");
  });

  it("rejects then passes on max_steps", () => {
    const s = spec("    - { id: s0, activity: a }\n    - { id: s1, activity: a }\n    - { id: s2, activity: a }\n    - { id: s3, activity: a }");
    const rejected = check(s, { max_steps: 3 });
    expect(rejected.status).toBe("failed");
    expect(rejected.message).toContain("flattened step count 4 exceeds composition ceiling 3");
    expect(check(s, { max_steps: 4 }).status).toBe("passed");
  });

  it("bounds a single workflow with max_total_steps (the closure walk owns the composed sum)", () => {
    const s = spec("    - { id: s0, activity: a }\n    - { id: s1, activity: a }\n    - { id: s2, activity: a }\n    - { id: s3, activity: a }");
    const rejected = check(s, { max_total_steps: 3 });
    expect(rejected.status).toBe("failed");
    expect(rejected.message).toContain("tree-wide composition ceiling max_total_steps 3");
    expect(check(s, { max_total_steps: 4 }).status).toBe("passed");
  });

  it("rejects then passes on max_parallel_width (tree-wide)", () => {
    const s = spec(`    - { id: s0, activity: a }\n${PARALLEL}`);
    const rejected = check(s, { max_parallel_width: 2 });
    expect(rejected.status).toBe("failed");
    expect(rejected.message).toContain("'par'");
    expect(rejected.message).toContain("width 3");
    expect(check(s, { max_parallel_width: 3 }).status).toBe("passed");
  });

  it("rejects then passes on max_parallel_nesting", () => {
    const nested = `    - id: outer
      parallel:
        branches:
          - id: ob
            steps:
              - id: inner
                parallel:
                  branches:
                    - { id: c1, steps: [{ id: c1s, activity: a }] }
                    - { id: c2, steps: [{ id: c2s, activity: a }] }
                  collect: { output: schemas:Out }
        collect: { output: schemas:Out }`;
    const s = spec(nested);
    const rejected = check(s, { max_parallel_nesting: 1 });
    expect(rejected.status).toBe("failed");
    expect(rejected.message).toContain("nesting depth 2");
    expect(check(s, { max_parallel_nesting: 2 }).status).toBe("passed");
  });

  it("gates map.workflow fan-out on allow_map_over_workflow", () => {
    const s = spec(`    - { id: s0, activity: a }\n${MAP_WF}`);
    const rejected = check(s, { allow_map_over_workflow: false });
    expect(rejected.status).toBe("failed");
    expect(rejected.message).toContain("'m'");
    expect(check(s, { allow_map_over_workflow: true }).status).toBe("passed");
    expect(check(spec("    - { id: s, activity: a }"), { allow_map_over_workflow: false }).status).toBe("passed");
  });
});

describe("composition merge (#298; most-restrictive)", () => {
  it("takes min of max_* and AND of allow_map_over_workflow", () => {
    const a = loadPolicySpec("name: a\ncomposition: { max_steps: 10, max_parallel_width: 4, allow_map_over_workflow: true }\n");
    const b = loadPolicySpec("name: b\ncomposition: { max_steps: 6, max_parallel_width: 8, allow_map_over_workflow: false }\n");
    const composed = composeProjectPolicies(
      [
        { id: "a", spec: a },
        { id: "b", spec: b },
      ],
      ["a", "b"],
    );
    expect(composed.payload["composition"]).toEqual({
      max_steps: 6,
      max_parallel_width: 4,
      allow_map_over_workflow: false,
    });
  });

  it("drops an unset composition from the payload (hash stability)", () => {
    const plain = loadPolicySpec("name: p\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n");
    const composed = composeProjectPolicies([{ id: "p", spec: plain }], ["p"]);
    expect(composed.payload["composition"]).toBeUndefined();
  });
});

describe("composition policy-load bounds (#298)", () => {
  it("rejects max_parallel_nesting above the in-spec hard ceiling of 3", () => {
    expect(() => loadPolicySpec("name: x\ncomposition: { max_parallel_nesting: 4 }\n")).toThrow(/may only tighten/);
    // <= the ceiling is accepted.
    expect(() => loadPolicySpec("name: x\ncomposition: { max_parallel_nesting: 3 }\n")).not.toThrow();
  });

  it("merges max_total_steps to the min", () => {
    const a = loadPolicySpec("name: a\ncomposition: { max_total_steps: 100 }\n");
    const b = loadPolicySpec("name: b\ncomposition: { max_total_steps: 40 }\n");
    const composed = composeProjectPolicies(
      [
        { id: "a", spec: a },
        { id: "b", spec: b },
      ],
      ["a", "b"],
    );
    expect(composed.payload["composition"]).toEqual({ max_total_steps: 40 });
  });

  it("rejects a zero ceiling with a message naming the alternatives (>= 1 contract)", () => {
    for (const field of ["max_steps", "max_total_steps", "max_parallel_width", "max_subworkflow_depth"]) {
      expect(() => loadPolicySpec(`name: x\ncomposition: { ${field}: 0 }\n`)).toThrow(
        /allow_map_over_workflow: false[\s\S]*validation targets/,
      );
    }
  });
});
