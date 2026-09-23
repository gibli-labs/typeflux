import { describe, expect, it } from "vitest";

import {
  composeProjectPolicyIds,
  loadPolicySpec,
  loadProjectSpec,
  ProjectPolicyError,
  resolveProjectPolicyClosure,
  validateProjectPolicyReferences,
  type ProjectPolicySources,
  type TypefluxProjectPolicySpec,
  type TypefluxProjectSpec,
} from "../src/index.js";

/** Build a policy-source map from inline policy YAML, keyed by name. */
function sourcesOf(...policies: string[]): ProjectPolicySources {
  const map: Record<string, TypefluxProjectPolicySpec> = {};
  for (const yaml of policies) {
    const spec = loadPolicySpec(yaml);
    map[spec.name] = spec;
  }
  return { policies: map };
}

/** A minimal project manifest DECLARING the given policy ids (compose resolves only declared policies). */
function projectWith(...policyIds: string[]): TypefluxProjectSpec {
  const policies = policyIds.length > 0 ? `policies:\n${policyIds.map((id) => `  ${id}: ${id}.yaml`).join("\n")}\n` : "";
  return loadProjectSpec(`version: "1"\nname: acme\nworkflows:\n  - id: w\n    path: a.yaml\n${policies}`);
}

const project = (body: string) => loadProjectSpec(`version: "1"\nname: acme\n${body}`);
const oneWorkflow = "workflows:\n  - id: review\n    path: a.yaml\n";

describe("resolveProjectPolicyClosure (#454 — extends closure)", () => {
  it("resolves an extends chain post-order (parents before children), deduped", () => {
    const sources = sourcesOf(
      "name: base\n",
      "name: mid\nextends: [base]\n",
      "name: top\nextends: [mid, base]\n",
    );
    const applied = resolveProjectPolicyClosure(projectWith("base", "mid", "top"), sources, ["top"]);
    // base before mid before top; base appears once despite two paths to it.
    expect(applied.map((a) => a.id)).toEqual(["base", "mid", "top"]);
  });

  it("an extended policy's constraints participate in the composed result", () => {
    // base allows only gpt-4o-mini; child extends it and adds nothing to providers,
    // so the composed allow-list still carries base's constraint.
    const sources = sourcesOf(
      "name: base\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n",
      "name: child\nextends: [base]\nobservability: { required: true }\n",
    );
    const composed = composeProjectPolicyIds(projectWith("base", "child"), sources, ["child"]);
    expect(composed.appliedPolicyIds).toEqual(["base", "child"]);
    expect((composed.payload as { providers: { allowed: unknown } }).providers.allowed).toEqual({
      openai: { models: ["gpt-4o-mini"] },
    });
    expect((composed.payload as { observability: { required: boolean } }).observability.required).toBe(true);
  });

  it("detects an extends cycle, including a self-extends", () => {
    const mutual = sourcesOf("name: a\nextends: [b]\n", "name: b\nextends: [a]\n");
    expect(() => resolveProjectPolicyClosure(projectWith("a", "b"), mutual, ["a"])).toThrow(
      /extends cycle detected: a -> b -> a/,
    );
    // Self-loop is the degenerate case — the appliedIds early-return must NOT mask it.
    expect(() => resolveProjectPolicyClosure(projectWith("a"), sourcesOf("name: a\nextends: [a]\n"), ["a"])).toThrow(
      /extends cycle detected: a -> a/,
    );
  });

  it("a duplicate selected id yields the SAME drift hash and effective constraints", () => {
    const sources = sourcesOf(
      "name: a\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n",
      "name: b\nobservability: { required: true }\n",
    );
    const proj = projectWith("a", "b");
    const once = composeProjectPolicyIds(proj, sources, ["a", "b"]);
    const dup = composeProjectPolicyIds(proj, sources, ["a", "b", "a"]);
    // The dup must not perturb the drift identity (canonicalHashPayload dedups).
    expect(dup.policyHash).toBe(once.policyHash);
    // The effective constraints match; only the recorded `selected_policy_ids` echoes
    // the caller's literal list (with the dup), matching Python's no-dedup on that field.
    const constraints = (payload: Record<string, unknown>): Record<string, unknown> => {
      const { selected_policy_ids: _s, ...rest } = payload;
      return rest;
    };
    expect(constraints(dup.payload as Record<string, unknown>)).toEqual(constraints(once.payload as Record<string, unknown>));
    expect((dup.payload as { selected_policy_ids: string[] }).selected_policy_ids).toEqual(["a", "b", "a"]);
  });

  it("rejects an unknown (undeclared) policy id and a name/id mismatch", () => {
    expect(() => resolveProjectPolicyClosure(projectWith("a"), sourcesOf("name: a\n"), ["missing"])).toThrow(
      /unknown project policy: missing/,
    );
    // Declared+provided under "wrong", but the spec's own name is "a".
    const mismatched: ProjectPolicySources = { policies: { wrong: loadPolicySpec("name: a\n") } };
    expect(() => resolveProjectPolicyClosure(projectWith("wrong"), mismatched, ["wrong"])).toThrow(
      /name must match the project policy id/,
    );
  });

  it("a prototype-named policy id resolves by OWN keys, not the prototype", () => {
    expect(() => resolveProjectPolicyClosure(projectWith("a"), sourcesOf("name: a\n"), ["toString"])).toThrow(
      /unknown project policy: toString/,
    );
  });

  it("composeProjectPolicyIds rejects an empty id list", () => {
    expect(() => composeProjectPolicyIds(projectWith("a"), sourcesOf("name: a\n"), [])).toThrow(
      /at least one project policy id/,
    );
  });
});

describe("validateProjectPolicyReferences (#454 — target + composition validation)", () => {
  const sources = sourcesOf(
    "name: org\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n",
    "name: eu\nproviders: { allowed: { openai: { models: [gpt-4o-mini, o1] } } }\n",
  );

  it("a well-formed project has no issues", () => {
    const spec = project(
      `${oneWorkflow}environments:\n  prod: e.yaml\npolicies:\n  org: p1.yaml\n  eu: p2.yaml\nvalidation:\n  targets:\n    ci:\n      workflows: [review]\n      environment: prod\n      policies: [org, eu]\n`,
    );
    expect(validateProjectPolicyReferences(spec, sources)).toEqual([]);
  });

  it("flags targets that reference unknown workflows, environments, or policies", () => {
    const spec = project(
      `${oneWorkflow}policies:\n  org: p1.yaml\nvalidation:\n  targets:\n    t:\n      workflows: [nope]\n      environment: ghost\n      policies: [org, missing]\n`,
    );
    const codes = validateProjectPolicyReferences(spec, sources).map((i) => i.code).sort();
    expect(codes).toEqual(["unknown_target_environment", "unknown_target_policy", "unknown_target_workflow"]);
  });

  it("flags a declared policy that fails to compose (bad extends)", () => {
    // `broken` extends `ghost`, which is neither declared nor provided → composition fails.
    const badSources = sourcesOf("name: broken\nextends: [ghost]\n");
    const spec = project(`${oneWorkflow}policies:\n  broken: p.yaml\n`);
    const issues = validateProjectPolicyReferences(spec, badSources);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toMatchObject({ code: "invalid_policy_composition", reference: "broken" });
    expect(issues[0]?.message).toMatch(/unknown project policy: ghost/);
  });

  it("flags a target whose two policies CONFLICT when composed together (disjoint allow-lists)", () => {
    // Each policy composes alone, but together their openai model allow-lists are disjoint.
    const conflicting = sourcesOf(
      "name: a\nproviders: { allowed: { openai: { models: [gpt-4o] } } }\n",
      "name: b\nproviders: { allowed: { openai: { models: [o1] } } }\n",
    );
    const spec = project(
      `${oneWorkflow}policies:\n  a: a.yaml\n  b: b.yaml\nvalidation:\n  targets:\n    t:\n      workflows: [review]\n      policies: [a, b]\n`,
    );
    const issues = validateProjectPolicyReferences(spec, conflicting);
    // No per-policy failure (each composes alone), only the target-set conflict.
    expect(issues.map((i) => i.code)).toEqual(["invalid_target_policy_composition"]);
    expect(issues[0]?.reference).toBe("t");
  });

  it("does not double-report a target whose member policy is individually invalid", () => {
    const badSources = sourcesOf("name: broken\nextends: [ghost]\n", "name: ok\n");
    const spec = project(
      `${oneWorkflow}policies:\n  broken: b.yaml\n  ok: o.yaml\nvalidation:\n  targets:\n    t:\n      workflows: [review]\n      policies: [broken, ok]\n`,
    );
    const issues = validateProjectPolicyReferences(spec, badSources);
    // Only the per-policy failure; the target-set composition is skipped (member invalid).
    expect(issues.map((i) => i.code)).toEqual(["invalid_policy_composition"]);
  });

  it("flags a declared policy whose source was not provided (missing_policy_source)", () => {
    const spec = project(`${oneWorkflow}policies:\n  org: p.yaml\n`);
    const issues = validateProjectPolicyReferences(spec, { policies: {} });
    expect(issues).toEqual([
      { code: "missing_policy_source", message: expect.stringMatching(/declared but no source/), reference: "org" },
    ]);
  });

  it("rejects an extends parent that is over-provided in sources but NOT declared in the manifest (Bugbot)", () => {
    // The manifest declares only `child`; `child` extends `base`, and the caller
    // over-provided `base` in sources. Python treats `base` as unknown (undeclared),
    // so the declared-only compose path must reject it — even via composeProjectPolicyIds.
    const overProvided = sourcesOf("name: child\nextends: [base]\n", "name: base\n");
    const spec = project(`${oneWorkflow}policies:\n  child: c.yaml\n`);
    const issues = validateProjectPolicyReferences(spec, overProvided);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toMatchObject({ code: "invalid_policy_composition", reference: "child" });
    expect(issues[0]?.message).toMatch(/unknown project policy: base/);
    // …and the exported compose seam itself refuses it (not just the validator).
    expect(() => composeProjectPolicyIds(spec, overProvided, ["child"])).toThrow(/unknown project policy: base/);
  });

  it("a target with an unknown policy AND an otherwise-conflicting valid pair reports ONLY the unknown code", () => {
    const conflicting = sourcesOf(
      "name: a\nproviders: { allowed: { openai: { models: [gpt-4o] } } }\n",
      "name: b\nproviders: { allowed: { openai: { models: [o1] } } }\n",
    );
    const spec = project(
      `${oneWorkflow}policies:\n  a: a.yaml\n  b: b.yaml\nvalidation:\n  targets:\n    t:\n      workflows: [review]\n      policies: [a, b, ghost]\n`,
    );
    expect(validateProjectPolicyReferences(spec, conflicting).map((i) => i.code)).toEqual(["unknown_target_policy"]);
  });

  it("the ProjectPolicyError type is exported and thrown by the composition seam", () => {
    expect(() => composeProjectPolicyIds(projectWith("a"), sourcesOf("name: a\n"), ["x"])).toThrow(ProjectPolicyError);
  });
});
