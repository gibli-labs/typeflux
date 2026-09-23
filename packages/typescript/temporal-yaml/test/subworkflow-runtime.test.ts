// Worker assembly over sub-workflow projects (#55 review round): the cross-workflow
// activity-name collision rule (identical definitions dedupe; divergent definitions reject
// naming both declaring workflows), the injected-sessionCache patch recursing into embedded
// child plans (#531 parity) with the child digest kept consistent, and the child
// activity-availability walk.

import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider } from "@typeflux/temporal";

import {
  assembleYamlRuntime,
  loadYamlSpec,
  projectSubworkflowResolver,
  workflowPlanDigest,
  type SubworkflowMapPlanStep,
  type SubworkflowPlanStep,
} from "../src/index.js";

const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };

const schemas = {
  "schemas:X": z.object({ id: z.string() }),
  "schemas:Batch": z.object({ items: z.array(z.object({ id: z.string() })) }),
};

/** A workflow spec declaring `defs` (activity definition lines; empty = `[]`) and `steps`. */
const specText = (name: string, defs: string, steps: string): string => `
project: p
name: ${name}
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities:
${defs === "" ? "  definitions: []" : `  definitions:\n${defs}`}
workflow:
  name: ${name}
  input: schemas:X
  output: schemas:X
  steps:
${steps}
`;

const DEF_A = '    - { name: shared, input: "schemas:X", output: "schemas:X", prompt: p/x }\n';
// Same name, DIFFERENT definition (another prompt) — the collision case.
const DEF_B = '    - { name: shared, input: "schemas:X", output: "schemas:X", prompt: p/other }\n';

const resolverFor = (selfId: string, specs: Record<string, string>) =>
  projectSubworkflowResolver(selfId, (id) => (id in specs ? loadYamlSpec(specs[id]!) : undefined));

describe("cross-workflow activity-name collisions (#55 review round)", () => {
  it("REJECTS a parent-vs-child collision with divergent definitions, naming both workflows", () => {
    const parent = loadYamlSpec(
      specText("Parent", DEF_A, "    - { id: own, activity: shared }\n    - { id: sub, workflow: child }\n"),
    );
    const specs = { child: specText("Child", DEF_B, "    - { id: run, activity: shared }\n") };
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor("parent", specs) }),
    ).toThrow(/"shared".*DIFFERENT definitions.*"parent".*"child"/s);
  });

  it("ALLOWS identical definitions across parent and child (one activity, declared twice)", () => {
    const parent = loadYamlSpec(
      specText("Parent", DEF_A, "    - { id: own, activity: shared }\n    - { id: sub, workflow: child }\n"),
    );
    const specs = { child: specText("Child", DEF_A, "    - { id: run, activity: shared }\n") };
    const { activities } = assembleYamlRuntime(parent, {
      provider,
      schemas,
      subworkflows: resolverFor("parent", specs),
    });
    expect(Object.keys(activities).filter((name) => name === "shared")).toHaveLength(1);
  });

  it("REJECTS a sibling-vs-sibling collision, naming both child workflow ids", () => {
    const parent = loadYamlSpec(
      specText("Parent", "", "    - { id: s1, workflow: c1 }\n    - { id: s2, workflow: c2 }\n"),
    );
    const specs = {
      c1: specText("C1", DEF_A, "    - { id: run, activity: shared }\n"),
      c2: specText("C2", DEF_B, "    - { id: run, activity: shared }\n"),
    };
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor("parent", specs) }),
    ).toThrow(/"shared".*DIFFERENT definitions.*"c1".*"c2"/s);
  });

  it("REJECTS a deep-chain (grandchild) collision with the parent", () => {
    const parent = loadYamlSpec(
      specText("Parent", DEF_A, "    - { id: own, activity: shared }\n    - { id: sub, workflow: mid }\n"),
    );
    const specs = {
      mid: specText("Mid", "", "    - { id: deeper, workflow: leaf }\n"),
      leaf: specText("Leaf", DEF_B, "    - { id: run, activity: shared }\n"),
    };
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor("parent", specs) }),
    ).toThrow(/"shared".*DIFFERENT definitions.*"parent".*"leaf"/s);
  });
});

describe("embedded child plans in worker assembly (#55 review round)", () => {
  it("patches injected sessionCache into a CHILD map step and recomputes the childDigest (#531 parity)", async () => {
    const { defineActivity } = await import("@typeflux/temporal");
    const cached = defineActivity({
      name: "code_cached",
      prompt: { name: "p/x", label: "production" },
      input: z.object({ id: z.string() }),
      output: z.object({ ok: z.boolean() }),
      sessionCache: { ttlSeconds: 600 },
    });
    const parent = loadYamlSpec(specText("Parent", "", "    - { id: sub, workflow: child }\n"));
    const childText = `
project: p
name: Child
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities: {}
workflow:
  name: Child
  input: schemas:Batch
  output: schemas:X
  steps:
    - id: fan
      map: { activity: code_cached, over: input.items }
    - id: reduce
      activity: code_cached
`;
    const { plan } = assembleYamlRuntime(parent, {
      provider,
      schemas,
      subworkflows: resolverFor("parent", { child: childText }),
      extraActivities: { code_cached: cached },
    });
    const node = plan.steps[0] as SubworkflowPlanStep;
    const childMap = node.plan.steps[0] as SubworkflowMapPlanStep & { sessionCache?: { enabled: boolean } };
    // The child's injected-cache map step got the sessionCache patch (not just the parent's).
    expect(childMap.sessionCache).toEqual({ enabled: true, ttlSeconds: 600 });
    // ... and the embedded child identity stayed consistent: childDigest matches the plan
    // the interpreter will actually dispatch to executeChild.
    expect(node.childDigest).toBe(workflowPlanDigest(node.plan));
  });

  it("rejects a CHILD step referencing an activity missing from the assembled map", () => {
    const parent = loadYamlSpec(specText("Parent", "", "    - { id: sub, workflow: child }\n"));
    // The child's step references an activity that is neither declared by any spec nor injected.
    const childText = `
project: p
name: Child
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities: {}
workflow:
  name: Child
  input: schemas:X
  output: schemas:X
  steps:
    - id: run
      activity: ghost_activity
`;
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor("parent", { child: childText }) }),
    ).toThrow(/sub-workflow "child" step "run" references activity "ghost_activity"/);
  });
});
