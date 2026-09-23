import { describe, expect, it } from "vitest";

import { diffTraceSummaries, type TraceSummary } from "./runDiff";

const base: TraceSummary = {
  workflow_contract_hash: "c1",
  manifest_hash: "m1",
  git_sha: "sha1",
  policy: { policy_hash: "p1" },
  provider_models: ["openai:gpt-4.1"],
  prompt_refs: ["assess"],
  activities: ["assess", "decide"],
  environment: "local",
  task_queue: "q",
  temporal_run_id: "run-a",
};

function withField(overrides: Partial<TraceSummary>): TraceSummary {
  return { ...base, ...overrides };
}

describe("diffTraceSummaries", () => {
  it("returns nothing for identical manifests (run id aside)", () => {
    expect(diffTraceSummaries(base, withField({ temporal_run_id: "run-a" }))).toEqual([]);
  });

  it("flags a contract-hash change as critical", () => {
    const [row] = diffTraceSummaries(base, withField({ workflow_contract_hash: "c2" }));
    expect(row).toMatchObject({ field: "workflow_contract_hash", severity: "critical", left: "c1", right: "c2" });
  });

  it("flags a code-sha change as critical", () => {
    const [row] = diffTraceSummaries(base, withField({ git_sha: "sha2" }));
    expect(row).toMatchObject({ field: "git_sha", severity: "critical" });
  });

  it("flags a policy-hash change as critical via the nested path", () => {
    const [row] = diffTraceSummaries(base, withField({ policy: { policy_hash: "p2" } }));
    expect(row).toMatchObject({ field: "policy.policy_hash", severity: "critical", left: "p1", right: "p2" });
  });

  it("flags a provider-model change as warning", () => {
    const [row] = diffTraceSummaries(base, withField({ provider_models: ["anthropic:claude"] }));
    expect(row).toMatchObject({ field: "provider_models", severity: "warning" });
  });

  it("flags a prompt-ref change as warning", () => {
    const [row] = diffTraceSummaries(base, withField({ prompt_refs: ["assess", "summarize"] }));
    expect(row).toMatchObject({ field: "prompt_refs", severity: "warning" });
  });

  it("treats arrays as sorted sets (order is not a difference)", () => {
    expect(diffTraceSummaries(base, withField({ activities: ["decide", "assess"] }))).toEqual([]);
  });

  it("orders rows critical before warning before info", () => {
    const rows = diffTraceSummaries(
      base,
      withField({ git_sha: "sha2", provider_models: ["x"], temporal_run_id: "run-b" }),
    );
    expect(rows.map((r) => r.severity)).toEqual(["critical", "warning", "info"]);
  });

  it("returns nothing when either side is missing (no trace)", () => {
    expect(diffTraceSummaries(base, null)).toEqual([]);
    expect(diffTraceSummaries(null, base)).toEqual([]);
  });
});
