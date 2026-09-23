import { describe, expect, it } from "vitest";

import type { Bundle, WorkflowSummary } from "./api";
import { deriveEnvironmentRows, deriveProfileSelectionRows, flattenOverrides, rowsRefreshing, rowsSettled } from "./environmentRows";
import type { BundleCell } from "./queries";

function bundle(overrides: {
  ok?: boolean;
  issues?: { code: string; message: string }[];
  policies?: string[] | null;
  digest?: string;
}): Bundle {
  return {
    validation: { ok: overrides.ok ?? true, issues: overrides.issues ?? [], checks: [] },
    policy:
      overrides.policies === null
        ? null
        : {
            selected_policy_ids: overrides.policies ?? ["base"],
            applied_policy_ids: overrides.policies ?? ["base"],
            policy_names: overrides.policies ?? ["base"],
            policy_hash: "hash-1",
          },
    secret_references: [],
    workflow: { spec_digest: overrides.digest ?? "d".repeat(64) },
  } as unknown as Bundle;
}

function cell(
  workflowId: string,
  env: string,
  overrides?: Parameters<typeof bundle>[0] | { unresolvable: true } | { pending: true },
): BundleCell {
  if (overrides && "unresolvable" in overrides) {
    return { workflowId, env, error: "422 does not resolve", pending: false, fetching: false, updatedAt: 1 };
  }
  if (overrides && "pending" in overrides) {
    return { workflowId, env, pending: true, fetching: true, updatedAt: 0 };
  }
  return { workflowId, env, bundle: bundle(overrides ?? {}), pending: false, fetching: false, updatedAt: 1 };
}

describe("deriveEnvironmentRows (#605)", () => {
  it("assembles per-workflow state in triage order — failing, unresolvable, pending, ok", () => {
    const rows = deriveEnvironmentRows("prod", [
      cell("healthy", "prod", { ok: true }),
      cell("broken", "prod", { ok: false, issues: [{ code: "policy_admission_failure", message: "m" }] }),
      cell("ghost", "prod", { unresolvable: true }),
      cell("slow", "prod", { pending: true }),
      cell("other-env", "staging", { ok: false }), // filtered out — not this environment
    ]);
    expect(rows.map((row) => [row.workflowId, row.state])).toEqual([
      ["broken", "failing"],
      ["ghost", "unresolvable"],
      ["slow", "pending"],
      ["healthy", "ok"],
    ]);
    expect(rows[0].detail).toBe("policy_admission_failure");
    expect(rows[1].detail).toBe("422 does not resolve");
    expect(rowsSettled(rows)).toBe(false);
  });

  it("carries policy ids + hash + digest and the worst-severity insight rollup", () => {
    const [row] = deriveEnvironmentRows("prod", [
      cell("w", "prod", { ok: false, issues: [{ code: "x", message: "m" }], policies: ["base", "strict"] }),
    ]);
    expect(row.policies).toEqual(["base", "strict"]);
    expect(row.policyHash).toBe("hash-1");
    expect(row.specDigest).toBe("d".repeat(64));
    // A failing bundle always yields at least the validation insight — the rollup reflects
    // it (an unrecognized issue code maps to warning in issueSeverity).
    expect(row.worstSeverity).toBe("warning");
    expect(row.insightCount).toBeGreaterThan(0);
  });

  it("keeps a resolved row's content during a background refetch (#604 conventions)", () => {
    const refreshing = { ...cell("w", "prod", { ok: false }), fetching: true };
    const [row] = deriveEnvironmentRows("prod", [refreshing]);
    expect(row.state).toBe("failing"); // cached verdict stays visible
    expect(rowsRefreshing([refreshing])).toBe(true); // ...with the still-refreshing hint
    expect(rowsRefreshing([cell("w", "prod", { ok: true })])).toBe(false);
  });
});

describe("deriveProfileSelectionRows (#605)", () => {
  const workflows = [
    { id: "review", profiles: { provider: "anthropic-prod" } },
    { id: "intake", profiles: {} },
  ] as unknown as WorkflowSummary[];

  it("marks a selection that replaces the workflow's manifest-level default", () => {
    const rows = deriveProfileSelectionRows(
      { review: { provider: "anthropic-staging" }, intake: { runtime: "hardened" } },
      workflows,
    );
    expect(rows).toEqual([
      {
        workflowId: "intake",
        kind: "runtime",
        selected: "hardened",
        differsFromDefault: false, // no manifest-level selection to replace
      },
      {
        workflowId: "review",
        kind: "provider",
        selected: "anthropic-staging",
        workflowDefault: "anthropic-prod",
        differsFromDefault: true,
      },
    ]);
  });

  it("does not mark a selection equal to the workflow default", () => {
    const rows = deriveProfileSelectionRows({ review: { provider: "anthropic-prod" } }, workflows);
    expect(rows[0].differsFromDefault).toBe(false);
  });
});

describe("flattenOverrides (#605)", () => {
  it("flattens nested override trees into sorted dotted-path rows", () => {
    expect(
      flattenOverrides({ task_queue: "prod-q", runtime: { temporal: { namespace: "prod" }, provider_retry: { max_attempts: 3 } } }),
    ).toEqual([
      ["runtime.provider_retry.max_attempts", "3"],
      ["runtime.temporal.namespace", '"prod"'],
      ["task_queue", '"prod-q"'],
    ]);
  });

  it("renders empty subtrees and handles no overrides", () => {
    expect(flattenOverrides({})).toEqual([]);
    expect(flattenOverrides({ runtime: {} })).toEqual([["runtime", "{}"]]);
    expect(flattenOverrides(undefined)).toEqual([]);
  });
});
