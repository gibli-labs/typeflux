import { describe, expect, it } from "vitest";

import type { Bundle } from "./api";
import { deriveEnvMatrix } from "./envMatrix";
import type { BundleCell } from "./queries";

function bundle(digest: string, policyHash: string | null, versionLabel?: string): Bundle {
  return {
    workflow: { spec_digest: digest, workflow_type: "typefluxYamlWorkflow", version_label: versionLabel ?? null },
    policy: policyHash === null ? null : { policy_hash: policyHash, selected_policy_ids: [], applied_policy_ids: [], policy_names: [] },
    validation: { ok: true, issues: [], checks: [] },
    secret_references: [],
  } as unknown as Bundle;
}

function cell(
  workflowId: string,
  env: string,
  overrides?: { digest?: string; policy?: string | null } | { unresolvable: true } | { pending: true },
): BundleCell {
  if (overrides && "unresolvable" in overrides) {
    return { workflowId, env, error: "422", pending: false, fetching: false, updatedAt: 1 };
  }
  if (overrides && "pending" in overrides) {
    return { workflowId, env, pending: true, fetching: true, updatedAt: 0 };
  }
  const { digest = "d1", policy = "p1" } = overrides ?? {};
  return { workflowId, env, bundle: bundle(digest, policy), pending: false, fetching: false, updatedAt: 1 };
}

describe("deriveEnvMatrix (#611)", () => {
  it("classifies match / spec / policy / both drift against the base", () => {
    const matrix = deriveEnvMatrix("prod", [
      cell("w", "prod", { digest: "d1", policy: "p1" }),
      cell("w", "same", { digest: "d1", policy: "p1" }),
      cell("w", "spec", { digest: "d2", policy: "p1" }),
      cell("w", "pol", { digest: "d1", policy: "p2" }),
      cell("w", "both", { digest: "d2", policy: "p2" }),
    ]);
    expect(matrix.settled).toBe(true);
    const byEnv = Object.fromEntries(matrix.rows[0].cells.map((c) => [c.env, c]));
    expect(byEnv["same"].state).toBe("match");
    expect(byEnv["spec"]).toMatchObject({ state: "drift", drift: { spec: true, policy: false, secret: false } });
    expect(byEnv["pol"]).toMatchObject({ state: "drift", drift: { spec: false, policy: true, secret: false } });
    expect(byEnv["both"]).toMatchObject({ state: "drift", drift: { spec: true, policy: true, secret: false } });
  });

  it("flags a secret-coverage difference as drift — the pairwise feed calls it critical (codex)", () => {
    const withSecret = (configured: boolean) =>
      ({
        workflow: { spec_digest: "d1" },
        policy: null,
        validation: { ok: true, issues: [], checks: [] },
        secret_references: [{ runtime_path: "runtime.provider.api_key", source_name: "KEY", configured }],
      }) as unknown as Bundle;
    const matrix = deriveEnvMatrix("prod", [
      { workflowId: "w", env: "prod", bundle: withSecret(true), pending: false, fetching: false, updatedAt: 1 },
      { workflowId: "w", env: "staging", bundle: withSecret(false), pending: false, fetching: false, updatedAt: 1 },
    ]);
    expect(matrix.rows[0].cells[0]).toMatchObject({ state: "drift", drift: { secret: true } });

    // A configured RENAME (same slot, different source, both configured) is info in the
    // pairwise feed — not matrix drift (codex).
    const renamed = (source: string) =>
      ({
        workflow: { spec_digest: "d1" },
        policy: null,
        validation: { ok: true, issues: [], checks: [] },
        secret_references: [{ runtime_path: "runtime.provider.api_key", source_name: source, configured: true }],
      }) as unknown as Bundle;
    const renameMatrix = deriveEnvMatrix("prod", [
      { workflowId: "w", env: "prod", bundle: renamed("KEY_PROD"), pending: false, fetching: false, updatedAt: 1 },
      { workflowId: "w", env: "staging", bundle: renamed("KEY_STG"), pending: false, fetching: false, updatedAt: 1 },
    ]);
    expect(renameMatrix.rows[0].cells[0].state).toBe("match");
  });

  it("flags a version-label split even when the graph digest matches (codex)", () => {
    const versioned = (label: string | undefined) =>
      ({
        workflow: { spec_digest: "d1", workflow_type: "typefluxYamlWorkflow", version_label: label ?? null },
        policy: null,
        validation: { ok: true, issues: [], checks: [] },
        secret_references: [],
      }) as unknown as Bundle;
    const matrix = deriveEnvMatrix("prod", [
      { workflowId: "w", env: "prod", bundle: versioned("v2"), pending: false, fetching: false, updatedAt: 1 },
      { workflowId: "w", env: "staging", bundle: versioned(undefined), pending: false, fetching: false, updatedAt: 1 },
    ]);
    expect(matrix.rows[0].cells[0]).toMatchObject({ state: "drift", drift: { spec: true } });
  });

  it("distinguishes different selections behind an empty (composition-failed) hash (codex)", () => {
    const failedComposition = (ids: string[]) =>
      ({
        workflow: { spec_digest: "d1" },
        policy: { policy_hash: "", selected_policy_ids: ids, applied_policy_ids: ids, policy_names: ids },
        validation: { ok: true, issues: [], checks: [] },
        secret_references: [],
      }) as unknown as Bundle;
    const matrix = deriveEnvMatrix("prod", [
      { workflowId: "w", env: "prod", bundle: failedComposition(["a"]), pending: false, fetching: false, updatedAt: 1 },
      { workflowId: "w", env: "staging", bundle: failedComposition(["b"]), pending: false, fetching: false, updatedAt: 1 },
    ]);
    expect(matrix.rows[0].cells[0]).toMatchObject({ state: "drift", drift: { policy: true } });
  });

  it("treats a both-none policy pair as a match (null-safe)", () => {
    const matrix = deriveEnvMatrix("prod", [
      cell("w", "prod", { policy: null }),
      cell("w", "staging", { policy: null }),
    ]);
    expect(matrix.rows[0].cells[0].state).toBe("match");
  });

  it("marks an unresolvable base row not-comparable — cells carry no drift verdict", () => {
    const matrix = deriveEnvMatrix("prod", [
      cell("w", "prod", { unresolvable: true }),
      cell("w", "staging", { digest: "d9" }),
    ]);
    expect(matrix.rows[0].comparable).toBe(false);
    expect(matrix.rows[0].cells[0].state).toBe("unresolvable");
    expect(matrix.notes).toEqual(["w does not resolve in prod — row not comparable"]);
  });

  it("reports unsettled while any cell first-loads and refreshing during background refetches", () => {
    const pendingMatrix = deriveEnvMatrix("prod", [
      cell("w", "prod"),
      cell("w", "staging", { pending: true }),
    ]);
    expect(pendingMatrix.settled).toBe(false);
    expect(pendingMatrix.rows[0].cells[0].state).toBe("pending");
    // No premature not-comparable note while unsettled.
    expect(pendingMatrix.notes).toEqual([]);

    const refreshingMatrix = deriveEnvMatrix("prod", [
      cell("w", "prod"),
      { ...cell("w", "staging", { digest: "d2" }), fetching: true },
    ]);
    expect(refreshingMatrix.settled).toBe(true);
    expect(refreshingMatrix.refreshing).toBe(true);
    expect(refreshingMatrix.rows[0].cells[0].state).toBe("drift"); // cached verdict stays
  });

  it("sorts drifted rows first, then not-comparable, then all-match", () => {
    const matrix = deriveEnvMatrix("prod", [
      cell("clean", "prod"),
      cell("clean", "staging"),
      cell("drifted", "prod"),
      cell("drifted", "staging", { digest: "d2" }),
      cell("ghost", "prod", { unresolvable: true }),
      cell("ghost", "staging"),
    ]);
    expect(matrix.rows.map((row) => row.workflowId)).toEqual(["drifted", "ghost", "clean"]);
  });
});
