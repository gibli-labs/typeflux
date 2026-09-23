// Sub-workflow surface (#55 slice 3): `workflow:` steps + `map.workflow` fan-out — plan
// derivation (embedded child plan + identity), the parent-digest FOLD (a child-graph edit
// moves the parent digest, an unrelated edit does not), project reference-cycle rejection,
// the standalone-spec rejection, and the schema chain over child input/output refs.

import { describe, expect, it } from "vitest";

import {
  projectSubworkflowResolver,
  workflowPlanFromSpec,
  workflowSchemaChainError,
  type SubworkflowSpecResolver,
} from "../src/build-workflow.js";
import { workflowPlanDigest } from "../src/frozen-version.js";
import { loadYamlSpec } from "../src/loader.js";
import type { SubworkflowMapPlanStep, SubworkflowPlanStep, TypefluxYamlSpec } from "../src/index.js";

/** A minimal single-activity child workflow spec (Item -> Assessment). */
const childSpecText = (assessActivity = "assess_item"): string => `
project: claims
name: claim_assessment
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions:
    - { name: ${assessActivity}, input: "schemas:Item", output: "schemas:Assessment", prompt: claims/assess }
workflow:
  name: ClaimAssessment
  input: schemas:Item
  output: schemas:Assessment
  steps:
    - id: assess
      activity: ${assessActivity}
`;

/** A parent that invokes the child by its manifest id `claim_assessment` as one step. */
const parentPlainText = `
project: claims
name: intake
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions: []
workflow:
  name: Intake
  input: schemas:Item
  output: schemas:Assessment
  steps:
    - id: assess_claim
      workflow: claim_assessment
`;

/** A parent that fans the child over `input.items` and collects. */
const parentMapText = `
project: claims
name: batch_intake
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions: []
workflow:
  name: BatchIntake
  input: schemas:ItemBatch
  output: schemas:AssessmentBatch
  steps:
    - id: assess_all
      map:
        workflow: claim_assessment
        over: input.items
        concurrency: 5
        collect: { output: schemas:AssessmentBatch, field: assessments }
`;

/** A resolver mapping manifest ids -> resolved specs (the project-manifest role). */
const resolverFor = (selfId: string, specs: Record<string, string>): SubworkflowSpecResolver =>
  projectSubworkflowResolver(selfId, (id) => (id in specs ? loadYamlSpec(specs[id]!) : undefined));

describe("sub-workflow plan derivation (#55 slice 3)", () => {
  it("embeds the child's resolved plan + identity into a `workflow:` step", () => {
    const parent = loadYamlSpec(parentPlainText);
    const plan = workflowPlanFromSpec(parent, {
      subworkflows: resolverFor("intake", { claim_assessment: childSpecText() }),
    });
    const step = plan.steps[0] as SubworkflowPlanStep;
    expect(step.kind).toBe("subworkflow");
    expect(step.id).toBe("assess_claim");
    expect(step.workflowId).toBe("claim_assessment"); // the MANIFEST reference
    expect(step.workflowName).toBe("ClaimAssessment"); // the child's OWN logical name
    expect(step.project).toBe("claims");
    // The child plan is embedded verbatim, and its digest is precomputed at resolve time
    // (the sandbox interpreter cannot run node:crypto).
    expect(step.plan.steps).toHaveLength(1);
    expect(step.childDigest).toBe(workflowPlanDigest(step.plan));
  });

  it("embeds identity + map fields into a `map.workflow` step", () => {
    const parent = loadYamlSpec(parentMapText);
    const plan = workflowPlanFromSpec(parent, {
      subworkflows: resolverFor("batch_intake", { claim_assessment: childSpecText() }),
    });
    const step = plan.steps[0] as SubworkflowMapPlanStep;
    expect(step.kind).toBe("subworkflowMap");
    expect(step.workflowId).toBe("claim_assessment");
    expect(step.over).toBe("input.items");
    expect(step.concurrency).toBe(5);
    expect(step.collectField).toBe("assessments");
    expect(step.childDigest).toBe(workflowPlanDigest(step.plan));
  });

  it("FOLDS child identity into the parent digest: a child-graph edit moves it, an identical child does not", () => {
    const digestFor = (childText: string): string => {
      const parent = loadYamlSpec(parentPlainText);
      return workflowPlanDigest(
        workflowPlanFromSpec(parent, {
          subworkflows: resolverFor("intake", { claim_assessment: childText }),
        }),
      );
    };
    const baseline = digestFor(childSpecText());
    // Re-resolving the SAME child yields the SAME parent digest (deterministic embedding).
    expect(digestFor(childSpecText())).toBe(baseline);
    // Editing the child's graph (a renamed activity is a different program) MOVES the parent
    // digest — the frozen-label cascade the design accepts.
    expect(digestFor(childSpecText("assess_item_v2"))).not.toBe(baseline);
  });

  it("rejects a project sub-workflow reference cycle A -> B -> A at load", () => {
    const aText = `
project: p
name: a
task_queue: q
runtime: { temporal: {}, registry: { type: inline }, provider: { type: openai } }
activities: { definitions: [] }
workflow:
  name: A
  input: schemas:X
  output: schemas:X
  steps:
    - id: call_b
      workflow: b
`;
    const bText = `
project: p
name: b
task_queue: q
runtime: { temporal: {}, registry: { type: inline }, provider: { type: openai } }
activities: { definitions: [] }
workflow:
  name: B
  input: schemas:X
  output: schemas:X
  steps:
    - id: call_a
      workflow: a
`;
    const a = loadYamlSpec(aText);
    expect(() =>
      workflowPlanFromSpec(a, { subworkflows: resolverFor("a", { a: aText, b: bText }) }),
    ).toThrow(/cycle/i);
  });

  it("rejects an undeclared sub-workflow reference, naming the manifest", () => {
    const parent = loadYamlSpec(parentPlainText);
    expect(() =>
      workflowPlanFromSpec(parent, { subworkflows: resolverFor("intake", {}) }),
    ).toThrow(/not declared in the project manifest/);
  });

  it("rejects a sub-workflow step in a STANDALONE spec (no resolver)", () => {
    const parent = loadYamlSpec(parentPlainText);
    expect(() => workflowPlanFromSpec(parent)).toThrow(/loaded standalone|project manifest/);
  });

  it("type-checks the chain against the child's workflow.input/output refs", () => {
    // classify (Disclosure -> Classification) then a child expecting Item is a mismatch.
    const mismatchParent = `
project: claims
name: intake_mismatch
task_queue: q
runtime: { temporal: {}, registry: { type: inline }, provider: { type: openai } }
activities:
  definitions:
    - { name: classify, input: "schemas:Disclosure", output: "schemas:Classification", prompt: claims/c }
workflow:
  name: IntakeMismatch
  input: schemas:Disclosure
  output: schemas:Assessment
  steps:
    - id: classify
      activity: classify
    - id: assess_claim
      workflow: claim_assessment
`;
    const spec = loadYamlSpec(mismatchParent);
    const subworkflows = resolverFor("intake_mismatch", { claim_assessment: childSpecText() });
    // The child expects schemas:Item, but the prior step yields schemas:Classification.
    expect(workflowSchemaChainError(spec, { subworkflows })).toMatch(/expecting input "schemas:Item"/);
  });
});

describe("sub-workflow resolution errors and memoization (#55 review round)", () => {
  it("a DECLARED-but-broken sibling surfaces its REAL resolution error, not 'not declared'", () => {
    const parent = loadYamlSpec(parentPlainText);
    const throwing: SubworkflowSpecResolver = {
      selfId: "intake",
      specFor: () => {
        throw new Error("environment variable OPENAI_API_KEY is not set");
      },
    };
    expect(() => workflowPlanFromSpec(parent, { subworkflows: throwing })).toThrow(
      /assess_claim.*claim_assessment.*failed to resolve.*OPENAI_API_KEY/s,
    );
    expect(() => workflowPlanFromSpec(parent, { subworkflows: throwing })).not.toThrow(/not declared/);
  });

  it("projectSubworkflowResolver memoizes: one underlying resolve per sibling id", () => {
    const calls: string[] = [];
    const resolver = projectSubworkflowResolver("intake", (id) => {
      calls.push(id);
      return id === "claim_assessment" ? loadYamlSpec(childSpecText()) : undefined;
    });
    resolver.specFor("claim_assessment");
    resolver.specFor("claim_assessment");
    resolver.specFor("nope");
    resolver.specFor("nope");
    expect(calls).toEqual(["claim_assessment", "nope"]);
  });

  it("a diamond reference graph resolves the shared grandchild ONCE (shared plan object)", () => {
    const leafText = `
project: p
name: d
task_queue: q
runtime: { temporal: {}, registry: { type: inline }, provider: { type: openai } }
activities:
  definitions: [{ name: leaf, input: "schemas:X", output: "schemas:X", prompt: p/x }]
workflow:
  name: D
  input: schemas:X
  output: schemas:X
  steps: [{ id: run, activity: leaf }]
`;
    const midText = (name: string): string => `
project: p
name: ${name}
task_queue: q
runtime: { temporal: {}, registry: { type: inline }, provider: { type: openai } }
activities: { definitions: [] }
workflow:
  name: ${name.toUpperCase()}
  input: schemas:X
  output: schemas:X
  steps: [{ id: call_d, workflow: d }]
`;
    const parentText = `
project: p
name: a
task_queue: q
runtime: { temporal: {}, registry: { type: inline }, provider: { type: openai } }
activities: { definitions: [] }
workflow:
  name: A
  input: schemas:X
  output: schemas:X
  steps:
    - { id: call_b, workflow: b }
    - { id: call_c, workflow: c }
`;
    const specs: Record<string, string> = { b: midText("b"), c: midText("c"), d: leafText };
    const resolveCounts = new Map<string, number>();
    // A RAW resolver (no projectSubworkflowResolver spec cache) so the count below measures the
    // derivation-wide identity memo, not the resolver-level memo.
    const raw: SubworkflowSpecResolver = {
      selfId: "a",
      specFor: (id) => {
        resolveCounts.set(id, (resolveCounts.get(id) ?? 0) + 1);
        return id in specs ? loadYamlSpec(specs[id]!) : undefined;
      },
    };
    const plan = workflowPlanFromSpec(loadYamlSpec(parentText), { subworkflows: raw });
    const bNode = plan.steps[0] as SubworkflowPlanStep;
    const cNode = plan.steps[1] as SubworkflowPlanStep;
    const dViaB = bNode.plan.steps[0] as SubworkflowPlanStep;
    const dViaC = cNode.plan.steps[0] as SubworkflowPlanStep;
    // The grandchild identity is memoized derivation-wide: both paths embed the SAME plan
    // object with the same digest (per-path re-derivation would be exponential in depth).
    expect(dViaB.plan).toBe(dViaC.plan);
    expect(dViaB.childDigest).toBe(dViaC.childDigest);
    // D's plan was BUILT once: the identity resolution consulted the resolver once for the
    // build (the per-step subworkflowRefs lookup adds one read per referencing step).
    expect(resolveCounts.get("d")).toBeLessThanOrEqual(3);
  });
});

describe("child identity memo — key-set drift guard (#55 review round)", () => {
  it("child memo keys = top-level identity memo keys + typeflux_parent_workflow_id", async () => {
    const { childIdentityMemo } = await import("../src/workflow-plan.js");
    const { workflowIdentityMemo } = await import("../src/frozen-version.js");
    const child = loadYamlSpec(childSpecText());
    const childPlan = workflowPlanFromSpec(child);
    const digest = workflowPlanDigest(childPlan);
    const topLevel = workflowIdentityMemo(child, digest);
    const asChild = childIdentityMemo(
      {
        workflowId: "claim_assessment",
        workflowName: child.workflow.name,
        project: child.project,
        childDigest: digest,
        plan: childPlan,
      },
      "parent-1",
    );
    // No version label on either side: the child set is exactly the top-level set + parent key.
    expect(new Set(Object.keys(asChild))).toEqual(
      new Set([...Object.keys(topLevel), "typeflux_parent_workflow_id"]),
    );
    // Values carry the child's OWN identity + the parent link.
    expect(asChild["typeflux_spec_digest"]).toBe(topLevel["typeflux_spec_digest"]);
    expect(asChild["typeflux_workflow"]).toBe(topLevel["typeflux_workflow"]);
    expect(asChild["typeflux_project"]).toBe(topLevel["typeflux_project"]);
    expect(asChild["typeflux_parent_workflow_id"]).toBe("parent-1");
    // With a version label both sides gain exactly typeflux_workflow_version.
    const labeled = loadYamlSpec(childSpecText().replace("  output: schemas:Assessment\n", "  output: schemas:Assessment\n  version: v1\n"));
    const labeledPlan = workflowPlanFromSpec(labeled);
    const labeledDigest = workflowPlanDigest(labeledPlan);
    const labeledTop = workflowIdentityMemo(labeled, labeledDigest);
    const labeledChild = childIdentityMemo(
      {
        workflowId: "claim_assessment",
        workflowName: labeled.workflow.name,
        project: labeled.project,
        childDigest: labeledDigest,
        versionLabel: "v1",
        plan: labeledPlan,
      },
      "parent-1",
    );
    expect(new Set(Object.keys(labeledChild))).toEqual(
      new Set([...Object.keys(labeledTop), "typeflux_parent_workflow_id"]),
    );
    expect(labeledChild["typeflux_workflow_version"]).toBe("v1");
  });
});
