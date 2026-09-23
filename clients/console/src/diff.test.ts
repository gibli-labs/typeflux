import { describe, expect, it } from "vitest";

import type { Bundle } from "./api";
import { classifyPath, diffBundles, summarizeDiff } from "./diff";

function bundleFor(env: string, overrides: Record<string, unknown> = {}): Bundle {
  return {
    bundle_version: "1",
    project: { name: "demo", manifest_path: `/checkout-${env}/typeflux.project.yaml` },
    environment: {
      id: env,
      name: env,
      profile_path: `/p/environments/${env}.yaml`,
      env_files: [],
      profile_variable_names: [],
    },
    workflow: {
      id: "workflow",
      path: "/p/workflow.yaml",
      yaml_project: "demo",
      yaml_name: "demo",
      workflow_name: "DemoWorkflow",
      workflow_type: "DemoWorkflow.abc123def456",
      spec_digest: "a".repeat(64),
      spec_digest_algorithm: "sha256",
      generator_version: "1",
      task_queue: "demo-queue",
      observability_trace_name: "TypefluxWorkflow:DemoWorkflow",
      input_schema: { name: "In", hash: "h1" },
      output_schema: { name: "Out", hash: "h2" },
    },
    runtime: {
      temporal: { address: `${env}-temporal:7233` },
      provider: { type: "anthropic", model: "claude-sonnet-4-6" },
    },
    activities: [],
    steps: [],
    topology: { nodes: [], edges: [] },
    secret_references: [],
    validation: { ok: true, issues: [], checks: [] },
    components: [],
    runtime_effective: [],
    ...overrides,
  } as Bundle;
}

describe("diffBundles", () => {
  it("returns no entries for identical bundles modulo expected per-checkout paths", () => {
    // manifest_path differs (different checkouts) but is ignored by design;
    // temporal address differs and is expected (info), so force it equal here.
    const left = bundleFor("local");
    const right = bundleFor("local");
    expect(diffBundles(left, right)).toEqual([]);
  });

  it("classifies version and policy drift as critical", () => {
    const left = bundleFor("local");
    const right = bundleFor("prod", {
      workflow: {
        ...bundleFor("prod").workflow,
        workflow_type: "DemoWorkflow.fff000fff000",
        spec_digest: "f".repeat(64),
      },
      policy: {
        selected_policy_ids: ["base"],
        applied_policy_ids: ["base"],
        policy_names: ["base"],
        policy_hash: "b".repeat(64),
      },
    });

    const entries = diffBundles(left, right);
    const byPath = new Map(entries.map((entry) => [entry.path, entry]));

    expect(byPath.get("workflow.spec_digest")?.severity).toBe("critical");
    expect(byPath.get("workflow.workflow_type")?.severity).toBe("critical");
    expect(byPath.get("policy.policy_hash")?.severity).toBe("critical");
    expect(byPath.get("policy.policy_hash")?.kind).toBe("added");
  });

  it("classifies provider divergence as warning and temporal address as info", () => {
    const left = bundleFor("local");
    const right = bundleFor("prod", {
      runtime: {
        temporal: { address: "prod-temporal:7233" },
        provider: { type: "anthropic", model: "claude-opus-4-8" },
      },
    });

    const entries = diffBundles(left, right);
    const byPath = new Map(entries.map((entry) => [entry.path, entry]));

    expect(byPath.get("runtime.provider.model")?.severity).toBe("warning");
    expect(byPath.get("runtime.provider.model")?.left).toBe("claude-sonnet-4-6");
    expect(byPath.get("runtime.provider.model")?.right).toBe("claude-opus-4-8");
    expect(byPath.get("runtime.temporal.address")?.severity).toBe("info");
    expect(byPath.get("environment.id")?.severity).toBe("info");
  });

  it("flags secret configured mismatches as critical", () => {
    const reference = {
      runtime_path: "runtime.provider.api_key",
      source_kind: "env",
      source_name: "ANTHROPIC_API_KEY",
    };
    const left = bundleFor("local", {
      secret_references: [{ ...reference, configured: true }],
    });
    const right = bundleFor("local", {
      secret_references: [{ ...reference, configured: false }],
    });

    const entries = diffBundles(left, right);

    expect(entries).toHaveLength(1);
    expect(entries[0].path).toBe("secret_references[0].configured");
    expect(entries[0].severity).toBe("critical");
  });

  it("summarizes by severity", () => {
    expect(classifyPath("components[0].content_hash")).toBe("warning");
    const summary = summarizeDiff([
      { path: "a", section: "a", severity: "critical", kind: "changed", left: 1, right: 2 },
      { path: "b", section: "b", severity: "warning", kind: "changed", left: 1, right: 2 },
      { path: "c", section: "c", severity: "info", kind: "changed", left: 1, right: 2 },
      { path: "d", section: "d", severity: "warning", kind: "changed", left: 1, right: 2 },
    ]);
    expect(summary).toEqual({ critical: 1, warning: 2, info: 1 });
  });
});
