import { loadYamlSpec, projectSubworkflowResolver, workflowPlanFromSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { buildBundleTopology } from "../src/index.js";

const RUNTIME = "runtime: { temporal: {}, registry: { type: inline, prompts: { p/x: hi } }, provider: { type: openai, model: gpt-4o-mini } }";
const DEFS =
  "activities:\n  definitions:\n" +
  "    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
  "    - { name: b, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
  "    - { name: c, input: schemas:In, output: schemas:Out, prompt: p/x }";

const spec = (workflowBody: string) => loadYamlSpec(`project: p\nname: n\ntask_queue: q\n${RUNTIME}\n${DEFS}\nworkflow:\n${workflowBody}`, { env: {} });

describe("buildBundleTopology — nodes + sequential edges (#563 slice 2b)", () => {
  it("projects one node per step (activity/map kind) and sequential edges in declared order", () => {
    const topology = buildBundleTopology(
      spec("  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n    - { id: m, map: { activity: b, over: s1 } }\n"),
    );
    expect(topology.nodes).toEqual([
      { id: "s1", kind: "activity", activity: "a" },
      { id: "m", kind: "map", activity: "b" },
    ]);
    // Sequential edges carry NO `condition` key (Python exclude_none on None).
    expect(topology.edges).toEqual([{ source: "s1", target: "m", kind: "sequential" }]);
  });
});

describe("buildBundleTopology — review edges (#563 slice 2b)", () => {
  // Uniform schema so review routing (checkpoint output → target input) type-checks in the plan.
  const reviewWorkflow = (enabled: boolean) =>
    loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\n${RUNTIME}\n` +
        "activities:\n  definitions:\n" +
        "    - { name: a, input: schemas:S, output: schemas:S, prompt: p/x }\n" +
        "    - { name: b, input: schemas:S, output: schemas:S, prompt: p/x }\n" +
        "    - { name: c, input: schemas:S, output: schemas:S, prompt: p/x }\n" +
        "workflow:\n  name: W\n  input: schemas:S\n" +
        `  lifecycle:\n    enabled: ${enabled}\n    review:\n      after_step: s1\n` +
        "      user_decisions:\n        escalate: { route: s3 }\n        approve: { route: s2 }\n" +
        "      timeout: { seconds: 60, on_timeout: route, route: s3 }\n" +
        "  steps:\n    - { id: s1, activity: a }\n    - { id: s2, activity: b }\n    - { id: s3, activity: c }\n",
      { env: {} },
    );

  it("adds review edges from the checkpoint to each routed decision (sorted) + the timeout route", () => {
    const topology = buildBundleTopology(reviewWorkflow(true));
    expect(topology.nodes.map((n) => n.id)).toEqual(["s1", "s2", "s3"]);
    expect(topology.edges).toEqual([
      { source: "s1", target: "s2", kind: "sequential" },
      { source: "s2", target: "s3", kind: "sequential" },
      // user_decisions sorted by decision: approve before escalate.
      { source: "s1", target: "s2", kind: "review", condition: "approve" },
      { source: "s1", target: "s3", kind: "review", condition: "escalate" },
      { source: "s1", target: "s3", kind: "review", condition: "timeout" },
    ]);
  });

  it("omits review edges when the lifecycle is not enabled (gate)", () => {
    const topology = buildBundleTopology(reviewWorkflow(false));
    expect(topology.edges.every((e) => e.kind === "sequential")).toBe(true);
  });
});

describe("buildBundleTopology — sub-workflow nodes (#55 slice 3)", () => {
  // A child (In -> Out) referenced by a parent as a plain step and a map fan-out.
  const childText =
    `project: p\nname: child\ntask_queue: q\n${RUNTIME}\n` +
    "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
    "workflow:\n  name: Child\n  input: schemas:In\n  output: schemas:Out\n  steps:\n    - { id: s, activity: a }\n";
  const subworkflows = projectSubworkflowResolver("parent", (id) =>
    id === "child" ? loadYamlSpec(childText, { env: {} }) : undefined,
  );

  it("projects a plain `workflow:` step to a `workflow` node carrying the child manifest id", () => {
    const parent = loadYamlSpec(
      `project: p\nname: parent\ntask_queue: q\n${RUNTIME}\nactivities: { definitions: [] }\n` +
        "workflow:\n  name: Parent\n  input: schemas:In\n  output: schemas:Out\n  steps:\n    - { id: sub, workflow: child }\n",
      { env: {} },
    );
    const topo = buildBundleTopology(parent, workflowPlanFromSpec(parent, { subworkflows }));
    expect(topo.nodes).toEqual([{ id: "sub", kind: "workflow", workflow: "child" }]);
    expect(topo.edges).toEqual([]);
  });

  it("projects a `map.workflow` fan-out to a `workflow` node too", () => {
    const parent = loadYamlSpec(
      `project: p\nname: parent\ntask_queue: q\n${RUNTIME}\nactivities: { definitions: [] }\n` +
        "workflow:\n  name: Parent\n  input: schemas:Batch\n  output: schemas:Batch\n  steps:\n" +
        "    - id: fan\n      map:\n        workflow: child\n        over: input.items\n        collect: { output: schemas:Batch, field: results }\n",
      { env: {} },
    );
    const topo = buildBundleTopology(parent, workflowPlanFromSpec(parent, { subworkflows }));
    expect(topo.nodes).toEqual([{ id: "fan", kind: "workflow", workflow: "child" }]);
  });
});

describe("buildBundleTopology — composition shapes (#55 slice 1)", () => {
  // The design's §3.1 shape: classify -> parallel(legal gated, medical) -> consolidate,
  // with an early-exit gate on a tail step.
  const compositionSpec = () =>
    loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\n${RUNTIME}\n` +
        "activities:\n  definitions:\n" +
        "    - { name: classify, input: schemas:S, output: schemas:S, prompt: p/x }\n" +
        "    - { name: legal, input: schemas:S, output: schemas:S, prompt: p/x }\n" +
        "    - { name: medical, input: schemas:S, output: schemas:S, prompt: p/x }\n" +
        "    - { name: consolidate, input: schemas:Bundle, output: schemas:Bundle, prompt: p/x }\n" +
        "workflow:\n  name: W\n  input: schemas:S\n  steps:\n" +
        "    - { id: classify_step, activity: classify }\n" +
        "    - id: reviews\n" +
        "      parallel:\n" +
        "        branches:\n" +
        "          - id: legal_branch\n" +
        "            when: { path: classify_step.needs_legal, eq: true }\n" +
        "            steps:\n" +
        "              - { id: legal_screen, activity: legal }\n" +
        "              - { id: legal_assess, activity: legal }\n" +
        "          - id: medical_branch\n" +
        "            steps: [{ id: medical_review, activity: medical }]\n" +
        "        collect: { output: schemas:Bundle }\n" +
        "    - { id: consolidate_step, activity: consolidate }\n" +
        "    - id: deep_analysis\n" +
        "      when: { path: classify_step.severity, gte: 3 }\n" +
        "      activity: consolidate\n",
      { env: {} },
    );

  it("projects parallel nodes (no activity key), branch/collect edges, and conditional edges", () => {
    const topology = buildBundleTopology(compositionSpec());
    expect(topology.nodes).toEqual([
      { id: "classify_step", kind: "activity", activity: "classify" },
      { id: "reviews", kind: "parallel" },
      { id: "legal_screen", kind: "activity", activity: "legal" },
      { id: "legal_assess", kind: "activity", activity: "legal" },
      { id: "medical_review", kind: "activity", activity: "medical" },
      { id: "consolidate_step", kind: "activity", activity: "consolidate" },
      { id: "deep_analysis", kind: "activity", activity: "consolidate" },
    ]);
    // The parallel node carries NO `activity` key at all (Python exclude_none).
    expect(Object.keys(topology.nodes[1]!)).toEqual(["id", "kind"]);
    expect(topology.edges).toEqual([
      { source: "classify_step", target: "reviews", kind: "sequential" },
      // Branch edges: block -> first branch step; condition = the branch's rendered when.
      { source: "reviews", target: "legal_screen", kind: "branch", condition: "classify_step.needs_legal == true" },
      { source: "legal_screen", target: "legal_assess", kind: "sequential" },
      // Collect edge: last branch step -> the block (where context[block.id] materializes).
      { source: "legal_assess", target: "reviews", kind: "collect" },
      { source: "reviews", target: "medical_review", kind: "branch" },
      { source: "medical_review", target: "reviews", kind: "collect" },
      { source: "reviews", target: "consolidate_step", kind: "sequential" },
      // A sequential edge INTO a when-gated step is conditional, carrying the predicate.
      { source: "consolidate_step", target: "deep_analysis", kind: "conditional", condition: "classify_step.severity >= 3" },
    ]);
  });

  it("V1 linear specs project byte-identically (no composition keys leak)", () => {
    const topology = buildBundleTopology(
      spec("  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n    - { id: s2, activity: b }\n"),
    );
    expect(JSON.stringify(topology)).toBe(
      '{"nodes":[{"id":"s1","kind":"activity","activity":"a"},{"id":"s2","kind":"activity","activity":"b"}],' +
        '"edges":[{"source":"s1","target":"s2","kind":"sequential"}]}',
    );
  });
});

describe("buildBundleTopology — code-activity step (#746)", () => {
  // A pure-code activity is INJECTED via extraActivities (no YAML definition), so it never appears
  // in the catalog/bundle activity list (the CP holds no code). But a workflow STEP referencing one
  // by name is structural — the topology node is an ordinary `activity` node, projected identically
  // to an AI step. `refine` is not declared under `definitions:`; the topology walk does not require
  // it (declaration checks live in the catalog/validate path, not the display projection).
  it("projects a step referencing an injected code activity as a plain `activity` node", () => {
    const topology = buildBundleTopology(
      spec("  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n    - { id: refine_step, activity: refine }\n"),
    );
    expect(topology.nodes).toEqual([
      { id: "s1", kind: "activity", activity: "a" },
      // The code-activity step is indistinguishable from an AI step in the structural topology.
      { id: "refine_step", kind: "activity", activity: "refine" },
    ]);
    expect(topology.edges).toEqual([{ source: "s1", target: "refine_step", kind: "sequential" }]);
  });
});
