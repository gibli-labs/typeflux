import { describe, expect, it } from "vitest";

import type { Bundle } from "./api";
import { deriveCrossEnvInsights } from "./crossEnv";
import type { BundleCell } from "./queries";

/** A minimal resolvable bundle; tests override only the compared fields. */
function bundle(overrides: {
  ok?: boolean;
  issues?: { code: string; message: string }[];
  policies?: string[] | null;
  secrets?: { runtime_path: string; source_name: string; configured: boolean }[];
}): Bundle {
  return {
    validation: {
      ok: overrides.ok ?? true,
      issues: overrides.issues ?? [],
      checks: [],
    },
    policy:
      overrides.policies === null
        ? null
        : {
            selected_policy_ids: overrides.policies ?? ["base"],
            applied_policy_ids: overrides.policies ?? ["base"],
            policy_names: overrides.policies ?? ["base"],
            policy_hash: "h",
          },
    secret_references: overrides.secrets ?? [],
  } as unknown as Bundle;
}

function cell(
  workflowId: string,
  env: string,
  overrides?: Parameters<typeof bundle>[0] | { unresolvable: true } | { pending: true },
): BundleCell {
  if (overrides && "unresolvable" in overrides) {
    return { workflowId, env, error: "422", pending: false, fetching: false, updatedAt: 1 };
  }
  if (overrides && "pending" in overrides) {
    return { workflowId, env, pending: true, fetching: true, updatedAt: 0 };
  }
  return { workflowId, env, bundle: bundle(overrides ?? {}), pending: false, fetching: false, updatedAt: 1 };
}

describe("deriveCrossEnvInsights — admission split (#604)", () => {
  it("flags a workflow that admits in one environment but fails in another, critical, linking the failing env", () => {
    const { insights } = deriveCrossEnvInsights([
      cell("review", "prod", { ok: true }),
      cell("review", "staging", { ok: false, issues: [{ code: "policy_admission_failure", message: "m" }] }),
    ]);
    expect(insights).toHaveLength(1);
    expect(insights[0]).toMatchObject({
      id: "crossenv:review:admission",
      severity: "critical",
      link: "#/workflows/review?env=staging&section=validation",
    });
    expect(insights[0].title).toContain("admits in prod but fails validation in staging");
    expect(insights[0].detail).toContain("policy_admission_failure");
  });

  it("stays silent when validation is uniform — all passing or all failing", () => {
    expect(
      deriveCrossEnvInsights([cell("w", "a", { ok: true }), cell("w", "b", { ok: true })]).insights,
    ).toEqual([]);
    expect(
      deriveCrossEnvInsights([cell("w", "a", { ok: false }), cell("w", "b", { ok: false })]).insights,
    ).toEqual([]);
  });
});

describe("deriveCrossEnvInsights — policy-selection split", () => {
  it("flags differing selected policies, naming each environment's set, linking the sparser env", () => {
    const { insights } = deriveCrossEnvInsights([
      cell("review", "prod", { policies: ["base", "regulated"] }),
      cell("review", "staging", { policies: null }),
    ]);
    expect(insights).toHaveLength(1);
    expect(insights[0]).toMatchObject({
      id: "crossenv:review:policy",
      severity: "warning",
      link: "#/workflows/review?env=staging&section=policy",
    });
    expect(insights[0].detail).toContain("prod: base, regulated");
    expect(insights[0].detail).toContain("staging: (none)");
  });

  it("treats identical sets as uniform regardless of order", () => {
    const { insights } = deriveCrossEnvInsights([
      cell("w", "a", { policies: ["p2", "p1"] }),
      cell("w", "b", { policies: ["p1", "p2"] }),
    ]);
    expect(insights).toEqual([]);
  });
});

describe("deriveCrossEnvInsights — secret-coverage split", () => {
  const ref = (configured: boolean) => [
    { runtime_path: "runtime.provider.api_key", source_name: "ANTHROPIC_API_KEY", configured },
  ];

  it("flags a slot configured in some environments but not others, linking the missing env's secrets", () => {
    const { insights } = deriveCrossEnvInsights([
      cell("review", "prod", { secrets: ref(true) }),
      cell("review", "staging", { secrets: ref(false) }),
    ]);
    expect(insights).toHaveLength(1);
    expect(insights[0]).toMatchObject({
      severity: "warning",
      link: "#/workflows/review?env=staging&section=secrets",
    });
    expect(insights[0].title).toContain("ANTHROPIC_API_KEY is configured in prod but not in staging");
  });

  it("flags a slot referenced in one environment but absent from another's resolution, as info (finder)", () => {
    const { insights } = deriveCrossEnvInsights([
      cell("review", "prod", { secrets: ref(true) }),
      cell("review", "staging", { secrets: [] }), // different dependency set entirely
    ]);
    expect(insights).toHaveLength(1);
    expect(insights[0]).toMatchObject({
      id: "crossenv:review:secret-wiring:runtime.provider.api_key|ANTHROPIC_API_KEY",
      severity: "info",
      link: "#/workflows/review?env=staging&section=secrets",
    });
    expect(insights[0].title).toContain("absent from staging's resolution");
  });

  it("stays silent when the slot is uniformly configured or uniformly missing", () => {
    expect(
      deriveCrossEnvInsights([
        cell("w", "a", { secrets: ref(true) }),
        cell("w", "b", { secrets: ref(true) }),
      ]).insights,
    ).toEqual([]);
    expect(
      deriveCrossEnvInsights([
        cell("w", "a", { secrets: ref(false) }),
        cell("w", "b", { secrets: ref(false) }),
      ]).insights,
    ).toEqual([]);
  });
});

describe("deriveCrossEnvInsights — outage honesty and gating", () => {
  it("treats an unresolvable environment as a note, never an insight, and still compares the resolved pair", () => {
    const feed = deriveCrossEnvInsights([
      cell("review", "prod", { ok: true }),
      cell("review", "staging", { ok: false }),
      cell("review", "dev", { unresolvable: true }),
    ]);
    expect(feed.notes).toEqual(["review does not resolve in dev"]);
    expect(feed.insights.map((insight) => insight.id)).toEqual(["crossenv:review:admission"]);
  });

  it("derives nothing while any cell is pending — no partial-fan-out flashes", () => {
    const feed = deriveCrossEnvInsights([
      cell("review", "prod", { ok: true }),
      cell("review", "staging", { pending: true }),
    ]);
    expect(feed).toEqual({ insights: [], notes: [], pending: true });
  });

  it("keeps deriving during a background refetch but reports pending (codex: stale-while-revalidate)", () => {
    const refreshing = { ...cell("review", "staging", { ok: false }), fetching: true };
    const feed = deriveCrossEnvInsights([cell("review", "prod", { ok: true }), refreshing]);
    expect(feed.pending).toBe(true); // still-comparing hint, never an all-clear
    expect(feed.insights.map((insight) => insight.id)).toEqual(["crossenv:review:admission"]);
  });

  it("treats a cell with stale data AND an error as unresolvable (codex: refetch failure keeps old data)", () => {
    const stale = { ...cell("review", "staging", { ok: false }), error: "503" };
    const feed = deriveCrossEnvInsights([cell("review", "prod", { ok: true }), stale]);
    expect(feed.insights).toEqual([]); // one resolved env left — nothing to compare
    expect(feed.notes).toEqual(["review does not resolve in staging"]);
  });

  it("links the environment with the fewest policies by COUNT, not fingerprint length (codex)", () => {
    const { insights } = deriveCrossEnvInsights([
      cell("w", "prod", { policies: ["p"] }),
      cell("w", "staging", { policies: null }),
    ]);
    expect(insights[0].link).toBe("#/workflows/w?env=staging&section=policy");
  });

  it("needs two resolved environments — single-env projects and lone survivors stay silent", () => {
    expect(deriveCrossEnvInsights([cell("w", "only", { ok: false })]).insights).toEqual([]);
    const lone = deriveCrossEnvInsights([
      cell("w", "a", { ok: false }),
      cell("w", "b", { unresolvable: true }),
    ]);
    expect(lone.insights).toEqual([]);
    expect(lone.notes).toEqual(["w does not resolve in b"]);
  });
});
