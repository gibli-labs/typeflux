import { describe, expect, it } from "vitest";

import type { Bundle, PolicyDefinition } from "./api";
import {
  compositionChain,
  deriveGovernanceCoverage,
  deriveGovernanceGaps,
  extendsProvenance,
  hasAssessableCoverage,
  policySections,
} from "./governance";
import type { BundleCell } from "./queries";

function cell(overrides: {
  workflowId: string;
  env: string;
  policy?: { selected: string[]; applied: string[]; hash: string } | null;
  unresolvable?: boolean;
}): BundleCell {
  const bundle = overrides.unresolvable
    ? undefined
    : ({
        policy:
          overrides.policy === null || overrides.policy === undefined
            ? null
            : {
                selected_policy_ids: overrides.policy.selected,
                applied_policy_ids: overrides.policy.applied,
                policy_hash: overrides.policy.hash,
              },
      } as unknown as Bundle);
  return {
    workflowId: overrides.workflowId,
    env: overrides.env,
    bundle,
    error: overrides.unresolvable ? "boom" : undefined,
    pending: false,
    fetching: false,
    updatedAt: 1,
  };
}

describe("deriveGovernanceCoverage", () => {
  it("classifies covered, none, composition-failed, and unresolvable cells", () => {
    const rows = deriveGovernanceCoverage([
      cell({ workflowId: "wf", env: "prod", policy: { selected: ["base"], applied: ["base"], hash: "h1" } }),
      cell({ workflowId: "wf", env: "dev", policy: null }),
      cell({ workflowId: "wf", env: "stage", policy: { selected: ["base", "x"], applied: [], hash: "" } }),
      cell({ workflowId: "wf", env: "cloud", unresolvable: true }),
    ]);
    expect(rows).toHaveLength(1);
    const byEnv = rows[0].byEnv;
    expect(byEnv.prod).toEqual({ state: "covered", appliedPolicyIds: ["base"], policyHash: "h1" });
    expect(byEnv.dev.state).toBe("none");
    expect(byEnv.stage.state).toBe("composition_failed");
    expect(byEnv.cloud.state).toBe("unresolvable");
  });

  it("treats selected-empty applied-empty as none, not composition failure", () => {
    const rows = deriveGovernanceCoverage([
      cell({ workflowId: "wf", env: "dev", policy: { selected: [], applied: [], hash: "" } }),
    ]);
    expect(rows[0].byEnv.dev.state).toBe("none");
  });
});

describe("deriveGovernanceGaps", () => {
  it("ranks composition failures above unprotected workflows with deep links", () => {
    const gaps = deriveGovernanceGaps(
      deriveGovernanceCoverage([
        cell({ workflowId: "wf_a", env: "dev", policy: null }),
        cell({ workflowId: "wf_b", env: "dev", policy: { selected: ["base"], applied: [], hash: "" } }),
      ]),
    );
    expect(gaps.map((gap) => gap.severity)).toEqual(["critical", "warning"]);
    expect(gaps[0].link).toBe("#/workflows/wf_b?env=dev&section=validation");
    expect(gaps[1].link).toBe("#/workflows/wf_a?env=dev&section=policy");
  });

  it("is empty when everything is covered (unresolvable is a note, not a gap)", () => {
    expect(
      deriveGovernanceGaps(
        deriveGovernanceCoverage([
          cell({ workflowId: "wf", env: "prod", policy: { selected: ["base"], applied: ["base"], hash: "h" } }),
          cell({ workflowId: "wf", env: "cloud", unresolvable: true }),
        ]),
      ),
    ).toEqual([]);
  });
});

describe("hasAssessableCoverage", () => {
  it("is false when every cell is unresolvable, true once anything resolves", () => {
    const allDown = deriveGovernanceCoverage([
      cell({ workflowId: "wf", env: "prod", unresolvable: true }),
      cell({ workflowId: "wf", env: "dev", unresolvable: true }),
    ]);
    expect(hasAssessableCoverage(allDown)).toBe(false);
    const oneUp = deriveGovernanceCoverage([
      cell({ workflowId: "wf", env: "prod", unresolvable: true }),
      cell({ workflowId: "wf", env: "dev", policy: null }),
    ]);
    expect(hasAssessableCoverage(oneUp)).toBe(true);
  });
});

describe("policySections", () => {
  it("orders known sections, keeps unknown ones, drops all-unset skeletons", () => {
    const sections = policySections({
      review: { require_review_routes: true, invalid_user_decision: null },
      providers: { allowed: { openai: { models: ["gpt-4.1"], base_urls: null } } },
      // The model dump serializes unset sections as all-null skeletons.
      observability: { required: null, allowed_backends: null, redaction: { required: null } },
      runtime: { temporal: { allowed_addresses: null, require_tls: null } },
      future_rule_kind: { anything: 1 },
    });
    expect(sections.map((section) => section.key)).toEqual([
      "providers",
      "review",
      "future_rule_kind",
    ]);
    expect(sections.find((section) => section.key === "future_rule_kind")?.known).toBe(false);
  });

  it("keeps explicit empties — deny-all is the most restrictive rule, never hidden", () => {
    // providers.allowed: {} blocks every provider; allowed_hosts: [] blocks
    // every host. The backend's composition preserves exactly these.
    const sections = policySections({
      providers: { allowed: {} },
      runtime: { registry: { allowed_hosts: [] }, temporal: { require_tls: null } },
    });
    expect(sections.map((section) => section.key)).toEqual(["providers", "runtime"]);
  });

  it("handles absent rules", () => {
    expect(policySections(null)).toEqual([]);
    expect(policySections(undefined)).toEqual([]);
  });
});

describe("compositionChain", () => {
  const def = (name: string, extendsIds: string[]): PolicyDefinition =>
    ({ name, extends: extendsIds, rules: {} }) as unknown as PolicyDefinition;

  it("orders transitive ancestors first, the policy last, dedup on diamonds", () => {
    const policies = new Map([
      ["root", def("root", [])],
      ["a", def("a", ["root"])],
      ["b", def("b", ["root"])],
      ["leaf", def("leaf", ["a", "b"])],
    ]);
    expect(compositionChain(policies, "leaf").map((policy) => policy.name)).toEqual([
      "root",
      "a",
      "b",
      "leaf",
    ]);
  });

  it("skips unknown references and survives cycles", () => {
    const policies = new Map([
      ["x", def("x", ["missing", "y"])],
      ["y", def("y", ["x"])],
    ]);
    expect(compositionChain(policies, "x").map((policy) => policy.name)).toEqual(["y", "x"]);
  });
});

describe("extendsProvenance", () => {
  const policy = (name: string, rules: Record<string, unknown>): PolicyDefinition =>
    ({ name, rules }) as unknown as PolicyDefinition;

  it("marks sections defined by multiple chain layers as overridden, in section order", () => {
    const provenance = extendsProvenance([
      policy("base", {
        providers: { allowed: { openai: { models: ["gpt-4o"] } } },
        imports: { allowed_module_roots: ["examples"] },
      }),
      policy("regulated", {
        providers: { allowed: { openai: { models: ["gpt-4.1"] } } },
        review: { require_review_routes: true },
      }),
    ]);
    expect(provenance).toEqual([
      { section: "providers", definedBy: ["base", "regulated"], overridden: true },
      { section: "review", definedBy: ["regulated"], overridden: false },
      { section: "imports", definedBy: ["base"], overridden: false },
    ]);
  });
});
