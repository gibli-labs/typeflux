import { describe, expect, it } from "vitest";

import type { components } from "@typeflux/control-plane-client";

import { latestPlanCoverage, workflowsWithoutPlan } from "./deploymentCoverage";

type DeploymentEntry = components["schemas"]["_DeploymentEntry"];
type PlanMismatch = components["schemas"]["PlanMismatch"];

function entry(opts: {
  workflowId: string;
  environmentId: string;
  generatedAt: string;
  ok: boolean;
  mismatches?: PlanMismatch[];
}): DeploymentEntry {
  const planHash = `${opts.workflowId}-${opts.environmentId}-${opts.generatedAt}`;
  return {
    plan_id: `${opts.workflowId}.${opts.environmentId}.${opts.generatedAt}`,
    promote_command: "deploy --apply …",

    path: "deployments/plan.yaml",
    plan: {
      plan_version: "1",
      plan_hash: planHash,
      generated_at: opts.generatedAt,
      identity: {
        workflow_id: opts.workflowId,
        workflow_name: opts.workflowId,
        workflow_type: `${opts.workflowId}.abc123`,
        spec_digest: "abc123",
        spec_digest_algorithm: "typeflux-yaml-graph-v1",
        environment_id: opts.environmentId,
      },
      policy: { selected_policy_ids: [], applied_policy_ids: [], policy_hash: "ph" },
      deployment: {
        image: "img@sha256:00",
        image_digest_pinned: true,
        preflight: { ok: true, issue_codes: [] },
      },
    },
    verification: { ok: opts.ok, mismatches: opts.mismatches ?? [] },
  };
}

describe("latestPlanCoverage", () => {
  it("classifies a single current plan as ready", () => {
    const rows = latestPlanCoverage([
      entry({ workflowId: "wf", environmentId: "local", generatedAt: "2026-01-01T00:00:00Z", ok: true }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ workflowId: "wf", environmentId: "local", state: "ready" });
  });

  it("classifies a drifted plan and carries the mismatches", () => {
    const mismatches: PlanMismatch[] = [
      { path: "identity.spec_digest", plan_value: "old", current_value: "new" },
    ];
    const rows = latestPlanCoverage([
      entry({
        workflowId: "wf",
        environmentId: "cloud",
        generatedAt: "2026-01-01T00:00:00Z",
        ok: false,
        mismatches,
      }),
    ]);
    expect(rows[0].state).toBe("drifted");
    expect(rows[0].mismatches).toEqual(mismatches);
  });

  it("keeps only the latest plan per (workflow, environment)", () => {
    const rows = latestPlanCoverage([
      entry({ workflowId: "wf", environmentId: "local", generatedAt: "2026-01-01T00:00:00Z", ok: false }),
      entry({ workflowId: "wf", environmentId: "local", generatedAt: "2026-06-01T00:00:00Z", ok: true }),
    ]);
    // The newer plan wins, so the target reads ready.
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ state: "ready", generatedAt: "2026-06-01T00:00:00Z" });
  });

  it("keeps distinct environments of the same workflow as separate rows, sorted", () => {
    const rows = latestPlanCoverage([
      entry({ workflowId: "wf", environmentId: "prod", generatedAt: "2026-01-01T00:00:00Z", ok: true }),
      entry({ workflowId: "wf", environmentId: "local", generatedAt: "2026-01-01T00:00:00Z", ok: true }),
    ]);
    expect(rows.map((r) => r.environmentId)).toEqual(["local", "prod"]);
  });
});

describe("workflowsWithoutPlan", () => {
  it("lists project workflows absent from every plan, sorted", () => {
    const entries = [
      entry({ workflowId: "covered", environmentId: "local", generatedAt: "2026-01-01T00:00:00Z", ok: true }),
    ];
    expect(workflowsWithoutPlan(entries, ["zeta", "covered", "alpha"])).toEqual(["alpha", "zeta"]);
  });

  it("returns nothing when every workflow has a plan", () => {
    const entries = [
      entry({ workflowId: "a", environmentId: "local", generatedAt: "2026-01-01T00:00:00Z", ok: true }),
    ];
    expect(workflowsWithoutPlan(entries, ["a"])).toEqual([]);
  });
});
