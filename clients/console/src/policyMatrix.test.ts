import { describe, expect, it } from "vitest";

import type { Bundle } from "./api";
import { derivePolicyMatrix } from "./policyMatrix";
import type { BundleCell } from "./queries";

function bundle(policies: string[] | null): Bundle {
  return {
    workflow: { spec_digest: "d1" },
    policy:
      policies === null
        ? null
        : { policy_hash: "h", selected_policy_ids: policies, applied_policy_ids: policies, policy_names: policies },
    validation: { ok: true, issues: [], checks: [] },
    secret_references: [],
  } as unknown as Bundle;
}

function cell(
  workflowId: string,
  env: string,
  overrides?: { policies?: string[] | null } | { unresolvable: true } | { pending: true },
): BundleCell {
  if (overrides && "unresolvable" in overrides) {
    return { workflowId, env, error: "422", pending: false, fetching: false, updatedAt: 1 };
  }
  if (overrides && "pending" in overrides) {
    return { workflowId, env, pending: true, fetching: true, updatedAt: 0 };
  }
  return {
    workflowId,
    env,
    bundle: bundle(overrides && "policies" in overrides ? (overrides.policies ?? null) : ["base"]),
    pending: false,
    fetching: false,
    updatedAt: 1,
  };
}

describe("derivePolicyMatrix (#612)", () => {
  it("classifies governed/ungoverned cells and flags split rows", () => {
    const matrix = derivePolicyMatrix([
      cell("w", "prod", { policies: ["base", "strict"] }),
      cell("w", "staging", { policies: null }),
    ]);
    expect(matrix.settled).toBe(true);
    const row = matrix.rows[0];
    expect(row.cells).toEqual([
      { env: "prod", state: "governed", ids: ["base", "strict"] },
      { env: "staging", state: "ungoverned", ids: [] },
    ]);
    expect(row.split).toBe(true);
    expect(row.ungoverned).toBe(true);
  });

  it("treats identical selections as uniform regardless of order and empty policy as ungoverned-uniform", () => {
    const uniform = derivePolicyMatrix([
      cell("w", "a", { policies: ["p2", "p1"] }),
      cell("w", "b", { policies: ["p1", "p2"] }),
    ]);
    expect(uniform.rows[0].split).toBe(false);

    const noneEverywhere = derivePolicyMatrix([
      cell("w", "a", { policies: null }),
      cell("w", "b", { policies: null }),
    ]);
    expect(noneEverywhere.rows[0].split).toBe(false);
    expect(noneEverywhere.rows[0].ungoverned).toBe(true);
  });

  it("classifies a failed composition as unenforced, not governed (codex)", () => {
    const failed = {
      workflow: { spec_digest: "d1" },
      policy: { policy_hash: "", selected_policy_ids: ["a", "b"], applied_policy_ids: [], policy_names: [] },
      validation: { ok: true, issues: [], checks: [] },
      secret_references: [],
    } as unknown as Bundle;
    const matrix = derivePolicyMatrix([
      { workflowId: "w", env: "prod", bundle: failed, pending: false, fetching: false, updatedAt: 1 },
    ]);
    expect(matrix.rows[0].cells[0]).toEqual({
      env: "prod",
      state: "composition-failed",
      ids: ["a", "b"],
    });
    expect(matrix.rows[0].ungoverned).toBe(true); // prioritized like a coverage gap
  });

  it("keeps unresolvable cells out of the split verdict", () => {
    const matrix = derivePolicyMatrix([
      cell("w", "prod", { policies: ["base"] }),
      cell("w", "dev", { unresolvable: true }),
    ]);
    expect(matrix.rows[0].split).toBe(false);
    expect(matrix.rows[0].cells[1]).toEqual({ env: "dev", state: "unresolvable", ids: [] });
  });

  it("gates on first-load pending and keeps verdicts during background refetches", () => {
    const pending = derivePolicyMatrix([cell("w", "a"), cell("w", "b", { pending: true })]);
    expect(pending.settled).toBe(false);

    const refreshing = derivePolicyMatrix([
      cell("w", "a"),
      { ...cell("w", "b", { policies: null }), fetching: true },
    ]);
    expect(refreshing.settled).toBe(true);
    expect(refreshing.refreshing).toBe(true);
    expect(refreshing.rows[0].cells[1].state).toBe("ungoverned"); // cached verdict stays
  });

  it("orders rows ungoverned-first, then split, then uniform", () => {
    const matrix = derivePolicyMatrix([
      cell("uniform", "a", { policies: ["p"] }),
      cell("uniform", "b", { policies: ["p"] }),
      cell("split", "a", { policies: ["p"] }),
      cell("split", "b", { policies: ["q"] }),
      cell("naked", "a", { policies: null }),
      cell("naked", "b", { policies: ["p"] }),
    ]);
    expect(matrix.rows.map((row) => row.workflowId)).toEqual(["naked", "split", "uniform"]);
  });
});
