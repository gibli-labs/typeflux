import { describe, expect, it } from "vitest";

import type { Bundle, PolicyDefinition } from "./api";
import type { CoverageRow } from "./governance";
import type { Insight } from "./insights";
import {
  coveragePercent,
  deriveCriticalDrift,
  deriveExecutiveRollup,
  deriveGovernanceRollup,
  deriveOperationsSummary,
  derivePolicySecurityControl,
  deriveSecurityPosture,
  deriveSecurityRollup,
} from "./personas";
import type { BundleCell, ExecutionsCell } from "./queries";

// ── shared cell builders ─────────────────────────────────────────────────────

function coverageRow(workflowId: string, byEnv: CoverageRow["byEnv"]): CoverageRow {
  return { workflowId, byEnv };
}

const covered = (ids: string[] = ["base"], hash = "h") =>
  ({ state: "covered" as const, appliedPolicyIds: ids, policyHash: hash });
const none = () => ({ state: "none" as const, appliedPolicyIds: [], policyHash: null });
const failed = () => ({ state: "composition_failed" as const, appliedPolicyIds: [], policyHash: null });
const unresolvable = () => ({ state: "unresolvable" as const, appliedPolicyIds: [], policyHash: null });

function bundleCell(overrides: Partial<BundleCell> & { workflowId: string; env: string }): BundleCell {
  return {
    workflowId: overrides.workflowId,
    env: overrides.env,
    bundle: overrides.bundle,
    error: overrides.error,
    pending: overrides.pending ?? false,
    fetching: overrides.fetching ?? false,
    updatedAt: overrides.updatedAt ?? 1,
  };
}

// ── governance rollup ─────────────────────────────────────────────────────────

describe("deriveGovernanceRollup", () => {
  it("buckets workflows and computes coveredPct over the assessed denominator", () => {
    const rollup = deriveGovernanceRollup([
      coverageRow("a", { prod: covered(), stage: covered() }), // governed
      coverageRow("b", { prod: covered(), stage: none() }), // partial
      coverageRow("c", { prod: none(), stage: none() }), // ungoverned
      coverageRow("d", { prod: failed(), stage: covered() }), // failed (outranks partial)
      coverageRow("e", { prod: unresolvable() }), // unassessable — excluded from denominator
    ]);
    expect(rollup.workflowCount).toBe(5);
    expect(rollup.assessedCount).toBe(4);
    expect(rollup.governedCount).toBe(1);
    expect(rollup.partialCount).toBe(1);
    expect(rollup.ungovernedCount).toBe(1);
    expect(rollup.failedCount).toBe(1);
    // 1 governed of 4 assessed = 25%.
    expect(rollup.coveredPct).toBe(25);
  });

  it("orders failed → ungoverned → partial → unassessable → governed and lists none/failed envs", () => {
    const rollup = deriveGovernanceRollup([
      coverageRow("z_governed", { prod: covered() }),
      coverageRow("a_failed", { stage: failed(), prod: none() }),
      coverageRow("m_ungoverned", { prod: none(), stage: none() }),
    ]);
    expect(rollup.rows.map((r) => r.workflowId)).toEqual([
      "a_failed",
      "m_ungoverned",
      "z_governed",
    ]);
    const failedRow = rollup.rows.find((r) => r.workflowId === "a_failed")!;
    expect(failedRow.status).toBe("failed");
    expect(failedRow.failedEnvs).toEqual(["stage"]);
    expect(failedRow.ungovernedEnvs).toEqual(["prod"]);
  });

  it("returns null coveredPct when nothing is assessable", () => {
    const rollup = deriveGovernanceRollup([coverageRow("a", { prod: unresolvable() })]);
    expect(rollup.assessedCount).toBe(0);
    expect(rollup.coveredPct).toBeNull();
  });
});

describe("coveragePercent (#721 F3 honest 100% boundary)", () => {
  it("reserves 100 for full coverage and floor-caps any shortfall at 99", () => {
    // The bug: Math.round(999/1000*100) = 100 → a false "every workflow governed" green.
    expect(coveragePercent(999, 1000)).toBe(99);
    expect(coveragePercent(1000, 1000)).toBe(100);
    // Ordinary buckets floor, and a fully-ungoverned project is 0 (not null).
    expect(coveragePercent(1, 4)).toBe(25);
    expect(coveragePercent(0, 3)).toBe(0);
    // Nothing assessable → null.
    expect(coveragePercent(0, 0)).toBeNull();
  });
});

// ── security posture ──────────────────────────────────────────────────────────

function securityBundle(runtime: Record<string, unknown>, extra: Partial<Bundle> = {}): Bundle {
  return {
    runtime,
    secret_references: [],
    workflow: { path: "wf/typeflux.yaml" },
    ...extra,
  } as unknown as Bundle;
}

describe("deriveSecurityPosture", () => {
  it("reads redaction/manifest/tls/provider-key defensively and marks absent fields unknown", () => {
    const rows = deriveSecurityPosture([
      bundleCell({
        workflowId: "wf",
        env: "prod",
        bundle: securityBundle({
          observability: { redaction_enabled: true, execution_manifest: true },
          temporal: { tls_enabled: false },
          // provider omitted entirely → provider_key is unknown, not a false off.
        }),
      }),
    ]);
    expect(rows).toHaveLength(1);
    const byKey = Object.fromEntries(rows[0].dimensions.map((d) => [d.key, d.state]));
    expect(byKey.redaction).toBe("on");
    expect(byKey.manifest).toBe("on");
    expect(byKey.tls).toBe("off");
    expect(byKey.provider_key).toBe("unknown");
    expect(byKey.secrets).toBe("na");
    expect(byKey.image_pin).toBe("na");
  });

  it("classifies secrets: na when none declared, on when all configured, off when any missing", () => {
    const withSecrets = (refs: unknown[]) =>
      securityBundle({}, { secret_references: refs } as Partial<Bundle>);
    const [none] = deriveSecurityPosture([
      bundleCell({ workflowId: "a", env: "e", bundle: withSecrets([]) }),
    ]);
    const [ok] = deriveSecurityPosture([
      bundleCell({
        workflowId: "b",
        env: "e",
        bundle: withSecrets([{ source_name: "OPENAI_API_KEY", configured: true }]),
      }),
    ]);
    const [missing] = deriveSecurityPosture([
      bundleCell({
        workflowId: "c",
        env: "e",
        bundle: withSecrets([
          { source_name: "OPENAI_API_KEY", configured: true },
          { source_name: "DB_URL", configured: false },
        ]),
      }),
    ]);
    expect(none.dimensions.find((d) => d.key === "secrets")!.state).toBe("na");
    expect(ok.dimensions.find((d) => d.key === "secrets")!.state).toBe("on");
    const missingDim = missing.dimensions.find((d) => d.key === "secrets")!;
    expect(missingDim.state).toBe("off");
    expect(missingDim.detail).toContain("DB_URL");
  });

  it("pins image state from the deployment preview, na when there is no preview", () => {
    const [pinned] = deriveSecurityPosture([
      bundleCell({
        workflowId: "a",
        env: "e",
        bundle: securityBundle({}, { deployment_preview: { image_digest_pinned: true } } as Partial<Bundle>),
      }),
    ]);
    const [mutable] = deriveSecurityPosture([
      bundleCell({
        workflowId: "b",
        env: "e",
        bundle: securityBundle({}, { deployment_preview: { image_digest_pinned: false } } as Partial<Bundle>),
      }),
    ]);
    const [absent] = deriveSecurityPosture([
      bundleCell({ workflowId: "c", env: "e", bundle: securityBundle({}) }),
    ]);
    expect(pinned.dimensions.find((d) => d.key === "image_pin")!.state).toBe("on");
    expect(mutable.dimensions.find((d) => d.key === "image_pin")!.state).toBe("off");
    expect(absent.dimensions.find((d) => d.key === "image_pin")!.state).toBe("na");
  });

  it("marks an unresolvable cell resolved=false with no dimensions (never a green posture)", () => {
    const [row] = deriveSecurityPosture([
      bundleCell({ workflowId: "wf", env: "cloud", error: "boom" }),
    ]);
    expect(row.resolved).toBe(false);
    expect(row.dimensions).toEqual([]);
  });
});

describe("deriveSecurityRollup", () => {
  it("counts only proven-off controls, never unknown/na", () => {
    const rows = deriveSecurityPosture([
      bundleCell({
        workflowId: "a",
        env: "e",
        bundle: securityBundle({
          observability: { redaction_enabled: false },
          temporal: { tls_enabled: false },
        }),
      }),
      bundleCell({
        workflowId: "b",
        env: "e",
        bundle: securityBundle({ observability: { redaction_enabled: true } }), // tls unknown
      }),
      bundleCell({ workflowId: "c", env: "e", error: "down" }),
    ]);
    const rollup = deriveSecurityRollup(rows);
    expect(rollup.resolvedRows).toBe(2);
    expect(rollup.unresolvedRows).toBe(1);
    expect(rollup.redactionOff).toBe(1);
    // b's tls is unknown (not reported), so it does NOT count as off — only a's does.
    expect(rollup.tlsOff).toBe(1);
  });
});

describe("derivePolicySecurityControl", () => {
  const policy = (rules: Record<string, unknown>): PolicyDefinition =>
    ({ id: "p", name: "p", rules }) as unknown as PolicyDefinition;

  it("detects redaction requirement, import allowlist, and artifact allowlist by presence", () => {
    const control = derivePolicySecurityControl(
      policy({
        observability: { redaction: { required: true } },
        imports: { allowed_module_roots: ["examples"] },
        artifacts: { allowed_sources: ["local_path"] },
      }),
      { blob: "x" },
    );
    expect(control.redactionRequired).toBe(true);
    // A DIRECT requirement is not tier-gated.
    expect(control.redactionTierGated).toBe(false);
    expect(control.hasImportAllowlist).toBe(true);
    expect(control.hasArtifactAllowlist).toBe(true);
    expect(control.sourceLinks).toEqual({ blob: "x" });
  });

  it("is all-false for a policy with no security rules", () => {
    const control = derivePolicySecurityControl(policy({ providers: { allowed: {} } }), {});
    expect(control.redactionRequired).toBe(false);
    expect(control.redactionTierGated).toBe(false);
    expect(control.hasImportAllowlist).toBe(false);
    expect(control.hasArtifactAllowlist).toBe(false);
  });

  it("reads redaction from a risk tier when the direct requirement is null (#721 F4, base-policy shape)", () => {
    // The bundled base policy: direct observability.redaction.required is null, but
    // risk_tiers.policy_gated.require_redaction is true (#300 fails closed on it) — the honest
    // per-policy answer is "required (risk-tier)", not "no".
    const control = derivePolicySecurityControl(
      policy({
        observability: { redaction: { required: null } },
        risk_tiers: {
          min_tier: "policy_gated",
          require_declared: null,
          safe: { require_redaction: null },
          policy_gated: { require_redaction: true, require_review: true },
          human_gated: { require_redaction: null },
          prohibited: { require_redaction: null },
        },
      }),
      {},
    );
    expect(control.redactionRequired).toBe(true);
    expect(control.redactionTierGated).toBe(true);
  });

  it("does not treat the risk_tiers scalar keys (min_tier, require_declared) as tier blocks", () => {
    const control = derivePolicySecurityControl(
      policy({ risk_tiers: { min_tier: "safe", require_declared: true } }),
      {},
    );
    expect(control.redactionRequired).toBe(false);
    expect(control.redactionTierGated).toBe(false);
  });
});

describe("deriveCriticalDrift (#721 F5)", () => {
  const insight = (severity: Insight["severity"], id: string): Insight => ({
    id,
    severity,
    title: id,
    detail: "",
    link: "#",
  });

  it("sums only CRITICAL findings across the three drift sources", () => {
    const count = deriveCriticalDrift({
      crossEnv: [insight("critical", "a"), insight("warning", "b"), insight("info", "c")],
      environmentDrift: [insight("critical", "d"), insight("critical", "e")],
      // derivePlanDrift mixes critical (drifted plan) with warning (no plan) — only criticals count.
      planDrift: [insight("critical", "f"), insight("warning", "g")],
    });
    expect(count).toBe(4);
  });

  it("is 0 when no source has a critical finding", () => {
    expect(
      deriveCriticalDrift({
        crossEnv: [insight("warning", "a")],
        environmentDrift: [],
        planDrift: [insight("warning", "b"), insight("info", "c")],
      }),
    ).toBe(0);
  });
});

// ── operations summary ────────────────────────────────────────────────────────

function execCell(
  workflowId: string,
  executions: Array<{ status: string; current_version?: boolean }>,
  error?: string,
): ExecutionsCell {
  return {
    workflowId,
    executions: executions.map((e, i) => ({
      execution_id: `${workflowId}-${i}`,
      status: e.status,
      current_version: e.current_version ?? true,
    })) as unknown as ExecutionsCell["executions"],
    error,
    pending: false,
  };
}

describe("deriveOperationsSummary", () => {
  it("counts failure-first classes, drain (old-version) runs, and unavailable workflows", () => {
    const summary = deriveOperationsSummary([
      execCell("a", [
        { status: "FAILED" },
        { status: "RUNNING", current_version: false },
        { status: "COMPLETED" },
      ]),
      execCell("b", [{ status: "TIMED_OUT" }, { status: "waiting_review" }]),
      execCell("c", [], "temporal unreachable"),
    ]);
    expect(summary.totalWorkflows).toBe(3);
    expect(summary.readableWorkflows).toBe(2);
    expect(summary.unavailableWorkflows).toEqual(["c"]);
    expect(summary.totalExecutions).toBe(5);
    expect(summary.failing).toBe(2); // FAILED + TimedOut
    expect(summary.running).toBe(2); // RUNNING + waiting_review
    expect(summary.completed).toBe(1);
    expect(summary.oldVersionRuns).toBe(1);
  });
});

// ── executive rollup ──────────────────────────────────────────────────────────

describe("deriveExecutiveRollup", () => {
  it("summarizes governance, validation health, review load, and drift in plain counts", () => {
    const governance = deriveGovernanceRollup([
      coverageRow("a", { local: covered() }),
      coverageRow("b", { local: none() }),
    ]);
    const rollup = deriveExecutiveRollup({
      workflowCount: 2,
      environmentCount: 3,
      policyCount: 1,
      governance,
      driftCount: 4,
      selectedEnvCells: [
        bundleCell({
          workflowId: "a",
          env: "local",
          bundle: {
            validation: { ok: true },
            lifecycle: { review: { after_step: "s" } },
          } as unknown as Bundle,
        }),
        bundleCell({
          workflowId: "b",
          env: "local",
          bundle: { validation: { ok: false } } as unknown as Bundle,
        }),
      ],
    });
    expect(rollup.workflowCount).toBe(2);
    expect(rollup.environmentCount).toBe(3);
    expect(rollup.underPolicyPct).toBe(50);
    expect(rollup.governedWorkflows).toBe(1);
    expect(rollup.ungovernedWorkflows).toBe(1);
    expect(rollup.validatingWorkflows).toBe(1);
    expect(rollup.workflowsWithIssues).toBe(1);
    expect(rollup.reviewGateWorkflows).toBe(1);
    expect(rollup.driftCount).toBe(4);
  });

  it("counts unresolvable selected-env workflows without inflating validation health", () => {
    const governance = deriveGovernanceRollup([coverageRow("a", { local: covered() })]);
    const rollup = deriveExecutiveRollup({
      workflowCount: 1,
      environmentCount: 1,
      policyCount: 0,
      governance,
      driftCount: 0,
      selectedEnvCells: [bundleCell({ workflowId: "a", env: "local", error: "down" })],
    });
    expect(rollup.validatingWorkflows).toBe(0);
    expect(rollup.unresolvableWorkflows).toBe(1);
  });
});
