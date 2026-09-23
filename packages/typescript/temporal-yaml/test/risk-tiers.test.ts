import { describe, expect, it } from "vitest";

import {
  assembleYamlRuntime,
  composeProjectPolicies,
  enforcePolicyCompliance,
  evaluateRiskTier,
  evaluateWorkflowRiskTier,
  loadPolicySpec,
  loadYamlSpec,
  ProjectPolicyEnforcementError,
  projectSubworkflowResolver,
  validatePolicyCompliance,
  validateSubworkflowClosurePolicy,
  workflowPlanDigest,
  workflowPlanFromSpec,
  type AppliedPolicy,
  type ComposedProjectPolicy,
  type SubworkflowClosureSpecResolver,
} from "../src/index.js";

// ── helpers (mirror policy-enforcement.test.ts) ─────────────────────────────

const applied = (id: string, yaml: string): AppliedPolicy => ({ id, spec: loadPolicySpec(`name: ${id}\n${yaml}`) });
const compose = (entries: AppliedPolicy[]): ComposedProjectPolicy =>
  composeProjectPolicies(entries, entries.map((e) => e.id));
const policyOf = (yaml: string): ComposedProjectPolicy => compose([applied("org", yaml)]);
const dig = (obj: unknown, ...keys: string[]): unknown =>
  keys.reduce<unknown>((o, k) => (o as Record<string, unknown> | undefined)?.[k], obj);
const check = (checks: ReturnType<typeof validatePolicyCompliance>, code: string) => checks.find((c) => c.code === code);

/**
 * A fully-parameterized workflow spec: `riskTier` sets `workflow.risk_tier`,
 * `provider`/`redaction`/`moderation`/`lifecycle` toggle the macro predicates.
 */
function riskSpec(opts: {
  riskTier?: string;
  provider?: string;
  model?: string;
  redaction?: boolean;
  moderation?: boolean;
  review?: boolean;
  /** `review: true` emits the gate under `enabled: <lifecycleEnabled ?? true>`. */
  lifecycleEnabled?: boolean;
  /** Mark activity `a` `side_effecting: true` (#299 D299-5 require_compensation). */
  sideEffecting?: boolean;
  /** Give step `s0` a `compensate:` running activity `a` (#299 D299-5). */
  compensate?: boolean;
  /** Declare `runtime.temporal.payload_codec` (#188 D188-2 require_payload_codec). */
  payloadCodec?: boolean;
} = {}): string {
  const providerType = opts.provider ?? "openai";
  const modelPart = opts.model !== undefined ? `, model: ${opts.model}` : "";
  const temporalBlock = opts.payloadCodec
    ? "\n    payload_codec:\n      type: aes\n      current: k1\n      keys:\n        - { id: k1, value_from: { env: TF_CODEC_KEY } }"
    : " {}";
  const redactionLine = opts.redaction === false ? "\n    redaction: { enabled: false }" : "";
  const moderationBlock = opts.moderation ? "\n      moderation: { provider: openai, on_violation: block }" : "";
  const sideEffectingLine = opts.sideEffecting ? "\n      side_effecting: true" : "";
  const compensateBlock = opts.compensate ? "\n      compensate:\n        activity: a" : "";
  const reviewBlock = opts.review
    ? `\n  lifecycle:\n    enabled: ${opts.lifecycleEnabled ?? true}\n    review:\n      after_step: s0\n      user_decisions: { approve: { route: s1 } }`
    : "";
  const secondStep = opts.review ? "\n    - id: s1\n      activity: a" : "";
  const riskLine = opts.riskTier !== undefined ? `\n  risk_tier: ${opts.riskTier}` : "";
  return `
project: p
name: w
task_queue: q
runtime:
  temporal:${temporalBlock}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: ${providerType}${modelPart} }
  observability:
    type: langfuse${redactionLine}
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x${moderationBlock}${sideEffectingLine}
workflow:
  name: W
  input: schemas:In${riskLine}${reviewBlock}
  steps:
    - id: s0
      activity: a${compensateBlock}${secondStep}
`;
}

const riskCheck = (yaml: string, policy: ComposedProjectPolicy) =>
  check(validatePolicyCompliance(loadYamlSpec(yaml), policy), "policy_risk_tier");

/** A human_gated workflow whose ONLY step is a `map:` fanning a side_effecting activity. */
function sideEffectingMapSpec(compensate: boolean): string {
  const compensateBlock = compensate ? "\n      compensate: { activity: a }" : "";
  return `
project: p
name: w
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
  observability: { type: langfuse }
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
      side_effecting: true
workflow:
  name: W
  input: schemas:In
  risk_tier: human_gated
  steps:
    - id: m0
      map:
        activity: a
        over: input.items
        concurrency: 1
        collect: { output: schemas:Out, field: results }${compensateBlock}
`;
}

/** A human_gated workflow whose side_effecting activity step is nested in a parallel branch. */
function sideEffectingParallelSpec(compensate: boolean): string {
  const compensateBlock = compensate ? "\n                compensate: { activity: a }" : "";
  return `
project: p
name: w
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
  observability: { type: langfuse }
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
      side_effecting: true
workflow:
  name: W
  input: schemas:In
  risk_tier: human_gated
  steps:
    - id: p0
      parallel:
        branches:
          - id: b0
            steps:
              - id: s0
                activity: a${compensateBlock}
        collect: { output: schemas:Out }
`;
}

// ── policy spec: enum validation + hash invariance ──────────────────────────

describe("risk_tiers policy dimension (#300)", () => {
  it("accepts the four ordered tiers and rejects an unknown tier", () => {
    for (const tier of ["safe", "policy_gated", "human_gated", "prohibited"]) {
      expect(() => loadPolicySpec(`name: p\nrisk_tiers: { min_tier: ${tier} }`)).not.toThrow();
    }
    expect(() => loadPolicySpec("name: p\nrisk_tiers: { min_tier: critical }")).toThrow(/min_tier/);
  });

  it("an unset risk_tiers block drops from the payload so policy_hash is stable", () => {
    const plain = policyOf("artifacts: { max_bytes: 1000 }");
    const governed = policyOf("artifacts: { max_bytes: 1000 }\nrisk_tiers: { min_tier: human_gated }");
    // The plain policy carries no risk_tiers key at all.
    expect(Object.hasOwn(plain.payload, "risk_tiers")).toBe(false);
    expect(dig(governed.payload, "risk_tiers", "min_tier")).toBe("human_gated");
  });
});

// ── merge rules ─────────────────────────────────────────────────────────────

describe("risk_tiers composition merge (#300)", () => {
  it("min_tier merges to the HIGHEST tier (ordered-enum, order-insensitive)", () => {
    const forward = compose([applied("org", "risk_tiers: { min_tier: policy_gated }"), applied("t", "risk_tiers: { min_tier: human_gated }")]);
    expect(dig(forward.payload, "risk_tiers", "min_tier")).toBe("human_gated");
    const reverse = compose([applied("org", "risk_tiers: { min_tier: human_gated }"), applied("t", "risk_tiers: { min_tier: policy_gated }")]);
    expect(dig(reverse.payload, "risk_tiers", "min_tier")).toBe("human_gated");
  });

  it("require_* flags OR and constrain_providers INTERSECTS (mapping shape, D300-6)", () => {
    // Shared provider keys survive; their models lists intersect — the same
    // mapping-allowlist rule as providers.allowed.
    const merged = compose([
      applied(
        "org",
        "risk_tiers: { human_gated: { require_review: false, constrain_providers: { openai: {}, anthropic: { models: [claude-sonnet-4-6, claude-haiku-4] } } } }",
      ),
      applied(
        "t",
        "risk_tiers: { human_gated: { require_review: true, constrain_providers: { anthropic: { models: [claude-haiku-4, claude-opus-4] }, gemini: {} } } }",
      ),
    ]);
    expect(dig(merged.payload, "risk_tiers", "human_gated", "require_review")).toBe(true);
    expect(dig(merged.payload, "risk_tiers", "human_gated", "constrain_providers")).toEqual({
      anthropic: { models: ["claude-haiku-4"] },
    });
  });
});

// ── spec: risk_tier field + digest invariance ───────────────────────────────

describe("workflow.risk_tier (#300)", () => {
  it("accepts the enum and rejects an unknown tier", () => {
    expect(() => loadYamlSpec(riskSpec({ riskTier: "human_gated" }))).not.toThrow();
    expect(() => loadYamlSpec(riskSpec({ riskTier: "critical" }))).toThrow();
  });

  it("is digest-invariant (governance metadata never enters the plan digest)", () => {
    const digestOf = (yaml: string) => workflowPlanDigest(workflowPlanFromSpec(loadYamlSpec(yaml)));
    const unset = digestOf(riskSpec());
    expect(digestOf(riskSpec({ riskTier: "safe" }))).toBe(unset);
    expect(digestOf(riskSpec({ riskTier: "human_gated" }))).toBe(unset);
    expect(digestOf(riskSpec({ riskTier: "prohibited" }))).toBe(unset);
    // #299 D299-5: side_effecting is governance metadata on a definition, never a digest
    // input — marking an activity side-effecting leaves the workflow type byte-identical.
    expect(digestOf(riskSpec({ sideEffecting: true }))).toBe(unset);
  });
});

// ── evaluate / validateRiskTier ─────────────────────────────────────────────

describe("validateRiskTier (#300, fail-closed macro expansion)", () => {
  it("skips when the policy declares no risk_tiers dimension", () => {
    const policy = policyOf("artifacts: { max_bytes: 10 }");
    expect(riskCheck(riskSpec(), policy)?.status).toBe("skipped");
    expect(evaluateRiskTier(loadYamlSpec(riskSpec()), policy.payload)).toBeUndefined();
  });

  it("a floor lifts the effective tier and expands the lifted block", () => {
    const policy = policyOf("risk_tiers: { min_tier: human_gated, human_gated: { require_review: true } }");
    const failed = riskCheck(riskSpec({ riskTier: "safe" }), policy);
    expect(failed?.status).toBe("failed");
    expect(dig(failed?.details, "effective")).toBe("human_gated");
    expect(dig(failed?.details, "floor_source")).toBe("policy_floor");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", review: true }), policy)?.status).toBe("passed");
  });

  it("require_declared rejects an undeclared workflow", () => {
    const policy = policyOf("risk_tiers: { require_declared: true }");
    expect(riskCheck(riskSpec(), policy)?.status).toBe("failed");
    expect(riskCheck(riskSpec({ riskTier: "safe" }), policy)?.status).toBe("passed");
  });

  it("effective prohibited denies admission (declared and via floor)", () => {
    // The dimension must be in play (an all-empty risk_tiers block drops on compose,
    // like every other dimension) — min_tier: safe is the minimal live block.
    const declared = policyOf("risk_tiers: { min_tier: safe }");
    expect(riskCheck(riskSpec({ riskTier: "prohibited" }), declared)?.status).toBe("failed");
    const floor = policyOf("risk_tiers: { min_tier: prohibited }");
    const check = riskCheck(riskSpec({ riskTier: "safe" }), floor);
    expect(check?.status).toBe("failed");
    expect(check?.message).toMatch(/denies admission/);
  });

  it("each require_* macro + constrain_providers, satisfied and unsatisfied", () => {
    const review = policyOf("risk_tiers: { human_gated: { require_review: true } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated" }), review)?.status).toBe("failed");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", review: true }), review)?.status).toBe("passed");

    const moderation = policyOf("risk_tiers: { human_gated: { require_moderation: true } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated" }), moderation)?.status).toBe("failed");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", moderation: true }), moderation)?.status).toBe("passed");

    const redaction = policyOf("risk_tiers: { human_gated: { require_redaction: true } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", redaction: false }), redaction)?.status).toBe("failed");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", redaction: true }), redaction)?.status).toBe("passed");

    // #188 D188-2: a human_gated tier implies require_payload_codec via the SAME macro
    // mechanism — a codec-less workflow fails, a codec-declared one passes.
    const codec = policyOf("risk_tiers: { human_gated: { require_payload_codec: true } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated" }), codec)?.status).toBe("failed");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", payloadCodec: true }), codec)?.status).toBe("passed");

    // D300-6: provider-ONLY constraint (empty allowance = any model of that provider).
    const providers = policyOf("risk_tiers: { human_gated: { constrain_providers: { anthropic: {} } } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", provider: "openai" }), providers)?.status).toBe("failed");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", provider: "anthropic" }), providers)?.status).toBe("passed");

    // #299 D299-5: require_compensation demands every side-effecting activity STEP
    // declare compensate:. Satisfied by declaring compensate: on the side-effecting step.
    const compensation = policyOf("risk_tiers: { human_gated: { require_compensation: true } }");
    const uncovered = riskCheck(riskSpec({ riskTier: "human_gated", sideEffecting: true }), compensation);
    expect(uncovered?.status).toBe("failed");
    expect(uncovered?.message).toMatch(/require_compensation/);
    expect(
      riskCheck(riskSpec({ riskTier: "human_gated", sideEffecting: true, compensate: true }), compensation)?.status,
    ).toBe("passed");
  });

  it("require_compensation ignores non-side-effecting steps (#299 D299-5)", () => {
    // An activity NOT marked side_effecting never trips the guard, even with no compensate:.
    const compensation = policyOf("risk_tiers: { human_gated: { require_compensation: true } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated" }), compensation)?.status).toBe("passed");
    // And a compensate-less side-effecting step under a tier that does NOT require
    // compensation is fine — the macro is opt-in per tier.
    const noReq = policyOf("risk_tiers: { human_gated: { require_review: false } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", sideEffecting: true }), noReq)?.status).toBe("passed");
  });

  it("require_compensation covers map steps and recurses parallel branches (#299 D299-5)", () => {
    // The map.activity and parallel-branch arms are a governance fail-open surface — pin
    // them. A side_effecting activity fanned by a map: (or nested in a parallel: branch)
    // without compensate fails; declaring compensate: satisfies it.
    const compensation = policyOf("risk_tiers: { human_gated: { require_compensation: true } }");
    expect(riskCheck(sideEffectingMapSpec(false), compensation)?.status).toBe("failed");
    expect(riskCheck(sideEffectingMapSpec(false), compensation)?.message).toMatch(/require_compensation/);
    expect(riskCheck(sideEffectingMapSpec(true), compensation)?.status).toBe("passed");
    expect(riskCheck(sideEffectingParallelSpec(false), compensation)?.status).toBe("failed");
    expect(riskCheck(sideEffectingParallelSpec(true), compensation)?.status).toBe("passed");
  });

  it("constrain_providers checks the MODEL like providers.allowed (D300-6)", () => {
    // The macro reuses the exact provider/model predicate, so a per-provider models
    // list rejects a disallowed model — not just a disallowed provider type.
    const policy = policyOf("risk_tiers: { human_gated: { constrain_providers: { openai: { models: [gpt-4o-mini] } } } }");
    expect(riskCheck(riskSpec({ riskTier: "human_gated", provider: "openai", model: "gpt-4o-mini" }), policy)?.status).toBe(
      "passed",
    );
    const badModel = riskCheck(riskSpec({ riskTier: "human_gated", provider: "openai", model: "gpt-4o" }), policy);
    expect(badModel?.status).toBe("failed");
    expect(badModel?.message).toMatch(/constrain_providers/);
  });

  it("require_review ignores inert gates under a disabled lifecycle (codex P1 parity)", () => {
    // A review block under enabled: false never executes — it must NOT satisfy
    // require_review (nor policy_review's require_review_routes) in either edition.
    const policy = policyOf("risk_tiers: { human_gated: { require_review: true } }\nreview: { require_review_routes: true }");
    const disabled = loadYamlSpec(riskSpec({ riskTier: "human_gated", review: true, lifecycleEnabled: false }));
    const disabledChecks = validatePolicyCompliance(disabled, policy);
    expect(check(disabledChecks, "policy_risk_tier")?.status).toBe("failed");
    expect(check(disabledChecks, "policy_review")?.status).toBe("failed");
    const enabled = loadYamlSpec(riskSpec({ riskTier: "human_gated", review: true, lifecycleEnabled: true }));
    const enabledChecks = validatePolicyCompliance(enabled, policy);
    expect(check(enabledChecks, "policy_risk_tier")?.status).toBe("passed");
    expect(check(enabledChecks, "policy_review")?.status).toBe("passed");
  });

  it("fails closed through enforcePolicyCompliance (admission gate)", () => {
    const policy = policyOf("risk_tiers: { min_tier: prohibited }");
    expect(() => enforcePolicyCompliance(loadYamlSpec(riskSpec({ riskTier: "safe" })), policy)).toThrow(
      ProjectPolicyEnforcementError,
    );
    expect(() => enforcePolicyCompliance(loadYamlSpec(riskSpec({ riskTier: "safe" })), policy)).toThrow(/denies admission/);
  });
});

// ── closure cascade ─────────────────────────────────────────────────────────

describe("risk-tier closure cascade (#300 D300-3)", () => {
  it("a safe parent embedding a human_gated child is lifted to human_gated and fails require_review", () => {
    const policy = policyOf("risk_tiers: { human_gated: { require_review: true } }");
    // Parent: safe, no review gate, references child.
    const parent = loadYamlSpec(`
project: p
name: parent
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities: { definitions: [] }
workflow:
  name: Parent
  input: schemas:In
  risk_tier: safe
  steps:
    - id: call
      workflow: child
`);
    // Child: human_gated, has its own review gate (so its own check passes).
    const child = loadYamlSpec(riskSpec({ riskTier: "human_gated", review: true }));
    const resolver: SubworkflowClosureSpecResolver = (id) => (id === "child" ? child : undefined);

    const closure = validateSubworkflowClosurePolicy(parent, policy, resolver, "parent");
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toMatch(/cascade/);
    expect(closure?.message).toMatch(/child/);
    const cascade = dig(closure?.details, "risk_tier_cascade");
    expect(dig(cascade, "parent_effective")).toBe("safe");
    expect(dig(cascade, "cascade_effective")).toBe("human_gated");
    expect(dig(cascade, "lifted_by")).toBe("child");
  });

  it("require_compensation rides the closure cascade: a child lift makes a parent's uncovered side effect fail (#299 D299-5)", () => {
    // require_compensation lifts through sub-workflows exactly like the other requirements —
    // it reuses the SAME cascade walk, no second traversal. A safe parent with an uncovered
    // side-effecting step is fine standalone, but once a human_gated child lifts it to a tier
    // that requires compensation, the parent's own uncovered step fails the closure check.
    const policy = policyOf("risk_tiers: { human_gated: { require_compensation: true } }");
    const parent = loadYamlSpec(`
project: p
name: parent
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
      side_effecting: true
workflow:
  name: Parent
  input: schemas:In
  risk_tier: safe
  steps:
    - id: s0
      activity: a
    - id: call
      workflow: child
`);
    // Child: human_gated, its own side-effecting step covered by compensate (its own check passes).
    const child = loadYamlSpec(riskSpec({ riskTier: "human_gated", sideEffecting: true, compensate: true }));
    const resolver: SubworkflowClosureSpecResolver = (id) => (id === "child" ? child : undefined);

    const closure = validateSubworkflowClosurePolicy(parent, policy, resolver, "parent");
    expect(closure?.status).toBe("failed");
    expect(closure?.message).toMatch(/require_compensation/);
    const cascade = dig(closure?.details, "risk_tier_cascade");
    expect(dig(cascade, "cascade_effective")).toBe("human_gated");
    expect(dig(cascade, "lifted_by")).toBe("child");
  });

  it("evaluateWorkflowRiskTier surfaces the same cascade the closure check computes (slice 2 posture)", () => {
    // The reusable posture that feeds the bundle's risk_tier — shares `riskTierCascade`
    // with admission, so a safe parent lifted by a human_gated child reports the lift.
    const policy = policyOf("risk_tiers: { human_gated: { require_review: true } }");
    const parent = loadYamlSpec(`
project: p
name: parent
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities: { definitions: [] }
workflow:
  name: Parent
  input: schemas:In
  risk_tier: safe
  steps:
    - id: call
      workflow: child
`);
    const child = loadYamlSpec(riskSpec({ riskTier: "human_gated", review: true }));
    const resolver: SubworkflowClosureSpecResolver = (id) => (id === "child" ? child : undefined);

    const posture = evaluateWorkflowRiskTier(parent, policy, resolver, "parent");
    expect(posture?.base.effective).toBe("safe"); // the parent's OWN declared+floor tier
    expect(posture?.cascade?.liftedBy).toBe("child");
    expect(posture?.cascade?.evaluation.effective).toBe("human_gated");
    expect(posture?.cascade?.evaluation.requirements).toEqual([
      { name: "require_review", satisfied: false },
    ]);

    // No resolver (standalone bundle) ⇒ base posture only, no cascade.
    const standalone = evaluateWorkflowRiskTier(parent, policy, undefined, "parent");
    expect(standalone?.base.effective).toBe("safe");
    expect(standalone?.cascade).toBeUndefined();

    // A policy with no risk_tiers dimension ⇒ no posture at all.
    expect(evaluateWorkflowRiskTier(parent, policyOf("artifacts: { max_bytes: 1 }"), resolver, "parent")).toBeUndefined();
  });
});

// ── SDK-entry pre-flight includes closure admission (finder fix) ─────────────

describe("assembleYamlRuntime pre-flight runs closure admission (#300/#298)", () => {
  const parentYaml = (steps: string, riskTier = "safe"): string => `
project: p
name: parent
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities: { definitions: [] }
workflow:
  name: Parent
  input: schemas:In
  risk_tier: ${riskTier}
  steps:
${steps}
`;

  it("a safe parent + human_gated child is refused at the SDK entry (cascade ran)", () => {
    const policy = policyOf("risk_tiers: { human_gated: { require_review: true } }");
    const parent = loadYamlSpec(parentYaml("    - id: call\n      workflow: child"));
    // Child: human_gated with its own ENABLED review gate, so the child's own
    // per-workflow checks pass — only the parent's cascade can refuse the build.
    const child = loadYamlSpec(riskSpec({ riskTier: "human_gated", review: true }));
    const subworkflows = projectSubworkflowResolver("parent", (id) => (id === "child" ? child : undefined));
    expect(() => assembleYamlRuntime(parent, { schemas: {}, policy, subworkflows })).toThrow(ProjectPolicyEnforcementError);
    expect(() => assembleYamlRuntime(parent, { schemas: {}, policy, subworkflows })).toThrow(/risk tier cascade from sub-workflow 'child'/);
  });

  it("tree-wide composition ceilings fire on the SDK entry (max_subworkflow_depth)", () => {
    // parent -> child -> grandchild = depth 2 > ceiling 1; only the closure walk
    // measures it, so this proves #298 tree-wide admission runs on this entry too.
    const policy = policyOf("composition: { max_subworkflow_depth: 1 }");
    const parent = loadYamlSpec(parentYaml("    - id: call\n      workflow: child"));
    const child = loadYamlSpec(`
project: p
name: child
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai }
activities: { definitions: [] }
workflow:
  name: Child
  input: schemas:In
  steps:
    - id: call
      workflow: grandchild
`);
    const grandchild = loadYamlSpec(riskSpec());
    const subworkflows = projectSubworkflowResolver("parent", (id) =>
      id === "child" ? child : id === "grandchild" ? grandchild : undefined,
    );
    expect(() => assembleYamlRuntime(parent, { schemas: {}, policy, subworkflows })).toThrow(
      /sub-workflow reference depth 2 exceeds composition ceiling 1/,
    );
  });

  it("a composed spec with NO resolver fails closed at the SDK entry (#699 rule)", () => {
    // Refs exist but no resolver reaches the closure check: indistinguishable from
    // "children never checked", so the pre-flight must refuse, never silently skip.
    const policy = policyOf("risk_tiers: { min_tier: safe }");
    const parent = loadYamlSpec(parentYaml("    - id: call\n      workflow: child"));
    expect(() => assembleYamlRuntime(parent, { schemas: {}, policy })).toThrow(/no\s+resolveSubworkflowSpec resolver was supplied/);
  });
});
