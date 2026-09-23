import { describe, expect, it } from "vitest";

import {
  buildProjectPolicyRuntimeGuard,
  loadPolicySpec,
  loadProjectSpec,
  loadYamlSpec,
  ProjectPolicyEnforcementError,
  RuntimePolicyGuard,
  selectProjectPolicyIdsForWorkflow,
  validateWorkflowPolicyCompliance,
  type ProjectPolicySources,
  type TypefluxProjectPolicySpec,
  type TypefluxYamlSpec,
} from "../src/index.js";

/** A project manifest whose validation targets bind policies to (workflow, environment) pairs. */
const PROJECT = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: a.yaml }
  - { id: other, path: b.yaml }
policies:
  strict: strict.yaml
  loose: loose.yaml
environments:
  prod: prod.yaml
  dev: dev.yaml
validation:
  targets:
    prod-review:
      workflows: [review]
      environment: prod
      policies: [strict]
    all-env-review:
      workflows: [review]
      policies: [loose]
`);

/** Build a policy-source map from inline policy YAML, keyed by name. */
function sourcesOf(...policies: string[]): ProjectPolicySources {
  const map: Record<string, TypefluxProjectPolicySpec> = {};
  for (const yaml of policies) {
    const spec = loadPolicySpec(yaml);
    map[spec.name] = spec;
  }
  return { policies: map };
}

/** A minimal, valid workflow spec using openai/gpt-4o-mini — the subject of enforcement. */
const WORKFLOW = `
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
  steps: [{ id: s, activity: a }]
`;
const spec: TypefluxYamlSpec = loadYamlSpec(WORKFLOW, { env: {} });

// `strict` admits exactly the workflow's model → compliant; `blocks-mini` admits only a
// different model → the workflow's gpt-4o-mini violates it.
const STRICT = "name: strict\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";
const LOOSE = "name: loose\nobservability: { required: false }\n";
const BLOCKS_MINI = "name: strict\nproviders: { allowed: { openai: { models: [gpt-4o] } } }\n";

describe("selectProjectPolicyIdsForWorkflow (#454; Python select_project_policy_ids_for_workflow)", () => {
  it("collects policies from every target that matches the (workflow, environment)", () => {
    // prod-review (env prod) + all-env-review (env unset → any) both list `review`.
    expect(selectProjectPolicyIdsForWorkflow(PROJECT, { environmentId: "prod", workflowId: "review" })).toEqual([
      "strict",
      "loose",
    ]);
  });

  it("skips a target whose environment does not match, keeps an environment-less target", () => {
    // In `dev`, prod-review (env prod) is skipped; all-env-review (env unset) still applies.
    expect(selectProjectPolicyIdsForWorkflow(PROJECT, { environmentId: "dev", workflowId: "review" })).toEqual([
      "loose",
    ]);
  });

  it("returns nothing for a workflow no target lists", () => {
    expect(selectProjectPolicyIdsForWorkflow(PROJECT, { environmentId: "prod", workflowId: "other" })).toEqual([]);
  });

  it("explicit ids win outright and are deduped in first-seen order", () => {
    expect(
      selectProjectPolicyIdsForWorkflow(PROJECT, {
        environmentId: "prod",
        workflowId: "review",
        explicitPolicyIds: ["loose", "strict", "loose"],
      }),
    ).toEqual(["loose", "strict"]);
  });

  it("dedupes policies contributed by more than one matching target", () => {
    const project = loadProjectSpec(`
version: "1"
name: acme
workflows: [{ id: review, path: a.yaml }]
policies: { p: p.yaml }
validation:
  targets:
    t1: { workflows: [review], policies: [p] }
    t2: { workflows: [review], policies: [p] }
`);
    expect(selectProjectPolicyIdsForWorkflow(project, { environmentId: "prod", workflowId: "review" })).toEqual(["p"]);
  });
});

describe("validateWorkflowPolicyCompliance (#454; _validate_resolved_workflow policy slice)", () => {
  it("returns skipped enforcement plus a passed risk_tier_binding when no policy is selected", () => {
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(), {
      spec,
      workflowId: "other", // no target lists `other`
      environmentId: "prod",
    });
    expect(checks).toEqual([
      { code: "policy_enforcement", status: "skipped", message: "no project policies selected for this workflow/environment" },
      // #788: an undeclared/safe tier passes unbound — only an elevated declaration fails.
      { code: "risk_tier_binding", status: "passed", message: "" },
    ]);
  });

  it("fails risk_tier_binding when an elevated declared tier has no policy selected (#788)", () => {
    const elevated = { ...spec, workflow: { ...spec.workflow, risk_tier: "human_gated" as const } };
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(), {
      spec: elevated,
      workflowId: "other",
      environmentId: "prod",
    });
    const binding = checks.find((check) => check.code === "risk_tier_binding");
    expect(binding?.status).toBe("failed");
    expect(binding?.message).toContain("UNENFORCED");
  });

  it("fails closed when an unbound parent references sub-workflows without a resolver (#788, Bugbot)", () => {
    const parent = {
      ...spec,
      workflow: {
        ...spec.workflow,
        steps: [...spec.workflow.steps, { id: "call", workflow: "child" }],
      },
    } as typeof spec;
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(), {
      spec: parent,
      workflowId: "other", // no target lists `other` -> nothing selected
      environmentId: "prod",
      // deliberately NO resolveSubworkflowSpec: an elevated child would be invisible
    });
    const binding = checks.find((check) => check.code === "risk_tier_binding");
    expect(binding?.status).toBe("failed");
    expect(binding?.message).toContain("cannot be verified");
  });

  it("fails risk_tier_binding when the bound policy declares no risk_tiers dimension (#788)", () => {
    const elevated = { ...spec, workflow: { ...spec.workflow, risk_tier: "policy_gated" as const } };
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(STRICT, LOOSE), {
      spec: elevated,
      workflowId: "reviewer",
      environmentId: "prod",
    });
    const binding = checks.find((check) => check.code === "risk_tier_binding");
    expect(binding?.status).toBe("failed");
    expect(binding?.message).toContain("risk_tiers dimension");
  });

  it("returns per-dimension checks (no failures) for a compliant workflow", () => {
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(STRICT, LOOSE), {
      spec,
      workflowId: "review",
      environmentId: "prod",
    });
    expect(checks.some((c) => c.code === "policy_selection" && c.status === "passed")).toBe(true);
    expect(checks.some((c) => c.code === "policy_allowlists" && c.status === "passed")).toBe(true);
    expect(checks.find((c) => c.code === "policy_provider")?.status).toBe("passed");
    expect(checks.some((c) => c.status === "failed")).toBe(false);
  });

  it("a VALID explicit override drives composition + compliance end-to-end (beats target selection)", () => {
    // `other` is listed by no target (target selection → none → would skip), so a real
    // per-dimension result here proves the explicit override is consumed by composition.
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(BLOCKS_MINI, LOOSE), {
      spec,
      workflowId: "other",
      environmentId: "prod",
      explicitPolicyIds: ["strict"],
    });
    expect(checks.some((c) => c.code === "policy_selection" && c.status === "passed")).toBe(true);
    // BLOCKS_MINI (keyed `strict`) forbids gpt-4o-mini → the override's policy actually enforces.
    expect(checks.find((c) => c.code === "policy_provider")?.status).toBe("failed");
  });

  it("surfaces a provider violation as a failed policy_provider check (does not throw)", () => {
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(BLOCKS_MINI, LOOSE), {
      spec,
      workflowId: "review",
      environmentId: "prod",
    });
    const provider = checks.find((c) => c.code === "policy_provider");
    expect(provider?.status).toBe("failed");
    expect(provider?.message).toMatch(/gpt-4o-mini/);
  });

  it("surfaces a composition failure as a failed policy_composition check", () => {
    // An explicit id the project does not declare → composeProjectPolicyIds throws → caught.
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf(STRICT), {
      spec,
      workflowId: "review",
      environmentId: "prod",
      explicitPolicyIds: ["nonexistent"],
    });
    expect(checks).toHaveLength(2);
    expect(checks[0]?.code).toBe("policy_composition");
    expect(checks[0]?.status).toBe("failed");
    // #788 Python parity: the binding verdict is skipped, never silently absent.
    expect(checks[1]).toEqual({ code: "risk_tier_binding", status: "skipped", message: "skipped because policy composition failed" });
  });
});

describe("buildProjectPolicyRuntimeGuard (#454; Python build_project_policy_runtime_guard)", () => {
  it("returns undefined when no policy is selected (no guard to install)", () => {
    expect(
      buildProjectPolicyRuntimeGuard(PROJECT, sourcesOf(), { spec, workflowId: "other", environmentId: "prod" }),
    ).toBeUndefined();
  });

  it("builds a guard from a VALID explicit override for a workflow no target lists", () => {
    const guard = buildProjectPolicyRuntimeGuard(PROJECT, sourcesOf(STRICT), {
      spec,
      workflowId: "other", // no target → explicit override is the only source
      environmentId: "prod",
      explicitPolicyIds: ["strict"],
    });
    expect(guard?.policy.selectedPolicyIds).toEqual(["strict"]);
  });

  it("returns a guard bound to the composed policy for a compliant workflow", () => {
    const guard = buildProjectPolicyRuntimeGuard(PROJECT, sourcesOf(STRICT, LOOSE), {
      spec,
      workflowId: "review",
      environmentId: "prod",
    });
    expect(guard).toBeInstanceOf(RuntimePolicyGuard);
    expect(guard?.policy.selectedPolicyIds).toEqual(["strict", "loose"]);
    // Bound to the spec provider identity — the workflow's own model still passes the guard.
    expect(() =>
      guard?.enforceProviderModel({
        providerName: "openai",
        model: "gpt-4o-mini",
        activityName: "a",
        promptName: "p/x",
      }),
    ).not.toThrow();
  });

  it("FAILS CLOSED: throws ProjectPolicyEnforcementError (carrying the checks) on a violation", () => {
    expect.assertions(3);
    try {
      buildProjectPolicyRuntimeGuard(PROJECT, sourcesOf(BLOCKS_MINI, LOOSE), {
        spec,
        workflowId: "review",
        environmentId: "prod",
      });
    } catch (error) {
      expect(error).toBeInstanceOf(ProjectPolicyEnforcementError);
      const enforcement = error as ProjectPolicyEnforcementError;
      expect(enforcement.message).toMatch(/^project policy enforcement failed: policy_provider:/);
      expect(enforcement.checks.some((c) => c.code === "policy_provider" && c.status === "failed")).toBe(true);
    }
  });
});

// ── transitive-closure admission (#55 slice 5, governance closure) ───────────

describe("transitive-closure admission (#55 §9)", () => {
  const CLOSURE_POLICY = `
version: "1"
name: only_mini
providers:
  allowed:
    openai:
      models: [gpt-4o-mini]
`;
  // A parent (compliant: gpt-4o-mini) that references a child via a `workflow:` step.
  const PARENT = `
project: p
name: parent
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: Parent
  input: schemas:In
  steps: [{ id: assess_one, workflow: child }]
`;
  const childYaml = (model: string): string => `
project: p
name: child
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: ${model} }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: Child
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
  const parentSpec = loadYamlSpec(PARENT, { env: {} });
  const sources = sourcesOf(CLOSURE_POLICY);
  const CLOSURE_PROJECT = loadProjectSpec(`
version: "1"
name: closure
workflows:
  - { id: parent, path: parent.yaml }
  - { id: child, path: child.yaml }
policies:
  only_mini: only_mini.yaml
validation:
  targets:
    all:
      workflows: [parent]
      policies: [only_mini]
`);

  it("rejects a parent whose referenced child violates the parent's policy", () => {
    const resolver = () => loadYamlSpec(childYaml("gpt-4o"), { env: {} });
    const checks = validateWorkflowPolicyCompliance(CLOSURE_PROJECT, sources, {
      spec: parentSpec,
      workflowId: "parent",
      environmentId: "prod",
      resolveSubworkflowSpec: resolver,
    });
    const closure = checks.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure).toBeDefined();
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toContain("child");
    expect(closure?.message).toContain("gpt-4o");
    // And it fails the parent's admission fail-closed.
    expect(() =>
      buildProjectPolicyRuntimeGuard(CLOSURE_PROJECT, sources, {
        spec: parentSpec,
        workflowId: "parent",
        environmentId: "prod",
        resolveSubworkflowSpec: resolver,
      }),
    ).toThrow(ProjectPolicyEnforcementError);
  });

  it("passes when the referenced child is compliant", () => {
    const resolver = () => loadYamlSpec(childYaml("gpt-4o-mini"), { env: {} });
    const checks = validateWorkflowPolicyCompliance(CLOSURE_PROJECT, sources, {
      spec: parentSpec,
      workflowId: "parent",
      environmentId: "prod",
      resolveSubworkflowSpec: resolver,
    });
    const closure = checks.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure?.status).toBe("passed");
    expect((closure?.details as { referenced_workflows: string[] }).referenced_workflows).toEqual(["child"]);
  });

  it("fails closed when a referenced child cannot be resolved (dangling ref)", () => {
    const checks = validateWorkflowPolicyCompliance(CLOSURE_PROJECT, sources, {
      spec: parentSpec,
      workflowId: "parent",
      environmentId: "prod",
      resolveSubworkflowSpec: () => undefined, // child cannot be resolved
    });
    const closure = checks.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toContain("child");
    expect(closure?.message).toContain("could not be resolved");
  });

  it("emits no closure check for a non-composed workflow (with or without a resolver)", () => {
    const nonComposed = validateWorkflowPolicyCompliance(CLOSURE_PROJECT, sources, {
      spec,
      workflowId: "parent",
      environmentId: "prod",
      resolveSubworkflowSpec: () => undefined,
    });
    expect(nonComposed.find((c) => c.code === "policy_subworkflow_closure")).toBeUndefined();
    const nonComposedNoResolver = validateWorkflowPolicyCompliance(CLOSURE_PROJECT, sources, {
      spec,
      workflowId: "parent",
      environmentId: "prod",
    });
    expect(nonComposedNoResolver.find((c) => c.code === "policy_subworkflow_closure")).toBeUndefined();
  });

  it("fails closed when a composed spec has no resolver (never silence)", () => {
    const noResolver = validateWorkflowPolicyCompliance(CLOSURE_PROJECT, sources, {
      spec: parentSpec,
      workflowId: "parent",
      environmentId: "prod",
    });
    const closure = noResolver.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toContain("resolveSubworkflowSpec");
    expect(closure?.message).toContain("'child'");
    // The guard builder fails closed on the same shape.
    expect(() =>
      buildProjectPolicyRuntimeGuard(CLOSURE_PROJECT, sources, {
        spec: parentSpec,
        workflowId: "parent",
        environmentId: "prod",
      }),
    ).toThrow(ProjectPolicyEnforcementError);
  });
});

describe("composition closure + sub-workflow depth (#298 Phase A)", () => {
  const workflowYaml = (name: string, steps: string): string => `
project: p
name: ${name}
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: ${name}
  input: schemas:In
  output: schemas:Out
  steps:
${steps}
`;
  const leafSteps = (count: number): string =>
    Array.from({ length: count }, (_, i) => `    - { id: s${i}, activity: a }`).join("\n");

  const PROJECT = loadProjectSpec(`
version: "1"
name: closure
workflows:
  - { id: root, path: root.yaml }
  - { id: mid, path: mid.yaml }
  - { id: leaf, path: leaf.yaml }
policies:
  comp: comp.yaml
validation:
  targets:
    all:
      workflows: [root]
      policies: [comp]
`);

  const rootSpec = loadYamlSpec(workflowYaml("root", "    - { id: call, workflow: mid }"), { env: {} });
  const specsById: Record<string, string> = {
    mid: workflowYaml("mid", "    - { id: call, workflow: leaf }"),
    leaf: workflowYaml("leaf", "    - { id: s0, activity: a }"),
  };
  const resolver = (id: string) => (specsById[id] !== undefined ? loadYamlSpec(specsById[id], { env: {} }) : undefined);

  it("fails when the reference tree is deeper than max_subworkflow_depth, naming the chain", () => {
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf("name: comp\ncomposition: { max_subworkflow_depth: 1 }\n"), {
      spec: rootSpec,
      workflowId: "root",
      environmentId: "prod",
      resolveSubworkflowSpec: resolver,
    });
    const closure = checks.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toContain("depth 2 exceeds composition ceiling 1");
    expect(closure?.message).toContain("root -> mid -> leaf");
  });

  it("passes at max_subworkflow_depth 2 and records the observed depth", () => {
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf("name: comp\ncomposition: { max_subworkflow_depth: 2 }\n"), {
      spec: rootSpec,
      workflowId: "root",
      environmentId: "prod",
      resolveSubworkflowSpec: resolver,
    });
    const closure = checks.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure?.status).toBe("passed");
    expect((closure?.details as { subworkflow_depth: number }).subworkflow_depth).toBe(2);
  });

  it("fails the parent when a closure MEMBER overflows a composition ceiling", () => {
    // Parent (root) has 1 step (passes max_steps=3); the child overflows it (5 steps).
    const bigChildResolver = (id: string) => (id === "mid" ? loadYamlSpec(workflowYaml("mid", leafSteps(5)), { env: {} }) : undefined);
    const checks = validateWorkflowPolicyCompliance(PROJECT, sourcesOf("name: comp\ncomposition: { max_steps: 3 }\n"), {
      spec: rootSpec,
      workflowId: "root",
      environmentId: "prod",
      resolveSubworkflowSpec: bigChildResolver,
    });
    const closure = checks.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toContain("sub-workflow 'mid'");
    expect(closure?.message).toContain("policy_composition_ceilings");
  });

  it("sums flattened steps tree-wide for max_total_steps (decomposition cannot evade)", () => {
    // Each member individually passes max_steps (root 1, mid 5 <= 5), but the sum (6)
    // exceeds max_total_steps 5 — the closure walk rejects the composed program.
    const fiveStepMid = (id: string) => (id === "mid" ? loadYamlSpec(workflowYaml("mid", leafSteps(5)), { env: {} }) : undefined);
    const checks = validateWorkflowPolicyCompliance(
      PROJECT,
      sourcesOf("name: comp\ncomposition: { max_steps: 5, max_total_steps: 5 }\n"),
      { spec: rootSpec, workflowId: "root", environmentId: "prod", resolveSubworkflowSpec: fiveStepMid },
    );
    const closure = checks.find((c) => c.code === "policy_subworkflow_closure");
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toContain("total step count 6 exceeds composition ceiling max_total_steps 5");
    expect(closure?.message).toContain("mid: 5");
    expect((closure?.details as { closure_total_steps: number }).closure_total_steps).toBe(6);

    // At exactly the sum it passes and records the observed total.
    const ok = validateWorkflowPolicyCompliance(
      PROJECT,
      sourcesOf("name: comp\ncomposition: { max_steps: 5, max_total_steps: 6 }\n"),
      { spec: rootSpec, workflowId: "root", environmentId: "prod", resolveSubworkflowSpec: fiveStepMid },
    );
    const okClosure = ok.find((c) => c.code === "policy_subworkflow_closure");
    expect(okClosure?.status).toBe("passed");
    expect((okClosure?.details as { closure_total_steps: number }).closure_total_steps).toBe(6);
  });
});
