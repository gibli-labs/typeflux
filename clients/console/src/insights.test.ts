import { describe, expect, it } from "vitest";

import type { Bundle, DrainStatus } from "./api";
import {
  deriveBundleInsights,
  deriveDrainInsights,
  drainStale,
  issueSeverity,
  validationIssueSourceLinks,
} from "./insights";
import type { RepoProvenance } from "./links";

function baseBundle(overrides: Partial<Bundle> = {}): Bundle {
  return {
    bundle_version: "1",
    project: { name: "demo", manifest_path: "/p/typeflux.project.yaml" },
    environment: {
      id: "local",
      name: "local",
      profile_path: "/p/environments/local.yaml",
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
      input_schema: {},
      output_schema: {},
    },
    runtime: {},
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

describe("deriveBundleInsights", () => {
  it("flags unconfigured secrets as critical with a secrets deep-link", () => {
    const bundle = baseBundle({
      secret_references: [
        {
          runtime_path: "runtime.provider.api_key",
          source_kind: "env",
          source_name: "ANTHROPIC_API_KEY",
          configured: false,
        },
      ],
    });

    const insights = deriveBundleInsights("workflow", "prod", bundle);

    expect(insights[0].severity).toBe("critical");
    expect(insights[0].title).toContain("ANTHROPIC_API_KEY");
    expect(insights[0].link).toBe("#/workflows/workflow?env=prod&section=secrets");
  });

  it("warns when no policy is applied and escalates failed composition", () => {
    const noPolicy = deriveBundleInsights("workflow", "local", baseBundle());
    expect(noPolicy.some((i) => i.severity === "warning" && i.id.endsWith("policy:none"))).toBe(
      true,
    );

    const failed = deriveBundleInsights(
      "workflow",
      "local",
      baseBundle({
        policy: {
          selected_policy_ids: ["base"],
          applied_policy_ids: [],
          policy_names: [],
          policy_hash: "",
        },
      }),
    );
    expect(failed[0].severity).toBe("critical");
    expect(failed[0].id.endsWith("policy:failed")).toBe(true);
  });

  it("warns on mutable deployment images and surfaces preview errors", () => {
    const mutable = deriveBundleInsights(
      "workflow",
      "local",
      baseBundle({
        deployment_preview: { target: "kubernetes", image_digest_pinned: false, workers: [] },
      }),
    );
    expect(
      mutable.some((i) => i.severity === "warning" && i.id.endsWith("mutable-image")),
    ).toBe(true);

    const errored = deriveBundleInsights(
      "workflow",
      "local",
      baseBundle({ deployment_preview: { error: "image must be pinned" } }),
    );
    expect(
      errored.some((i) => i.severity === "warning" && i.id.endsWith("deployment:error")),
    ).toBe(true);
  });

  it("notes warn-mode review gates as info", () => {
    const bundle = baseBundle({
      lifecycle: {
        enabled: true,
        progress: true,
        cancellation: true,
        status_event_limit: 50,
        review: {
          after_step: "assess",
          invalid_user_decision: "warn",
          user_decisions: { approve: "decide" },
        },
      },
    });

    const insights = deriveBundleInsights("workflow", "local", bundle);
    const reviewInsight = insights.find((i) => i.id.endsWith("review:warn-mode"));

    expect(reviewInsight?.severity).toBe("info");
    expect(reviewInsight?.link).toContain("section=lifecycle");
  });

  it("ranks validation severity by issue code", () => {
    expect(issueSeverity({ code: "missing_workflow_file" })).toBe("critical");
    expect(issueSeverity({ code: "unknown_profile_reference" })).toBe("critical");
    expect(issueSeverity({ code: "docs_drift" })).toBe("warning");
  });

  it("surfaces a satisfied risk tier as info (control names only, no secrets)", () => {
    const bundle = baseBundle({
      risk_tier: {
        declared: "policy_gated",
        effective: "policy_gated",
        floor: "policy_gated",
        floor_source: "declared",
        requirements: [
          { name: "require_review", satisfied: true },
          { name: "require_redaction", satisfied: true },
        ],
      },
    } as unknown as Partial<Bundle>);

    const insight = deriveBundleInsights("workflow", "local", bundle).find((i) =>
      i.id.endsWith("risk-tier"),
    );

    expect(insight?.severity).toBe("info");
    expect(insight?.title).toBe("Risk tier: policy_gated");
    expect(insight?.link).toContain("section=policy");
  });

  it("escalates an unsatisfied risk-tier requirement to critical (admission denial) and names the control", () => {
    const bundle = baseBundle({
      risk_tier: {
        declared: "safe",
        effective: "human_gated",
        floor: "human_gated",
        floor_source: "policy_floor",
        requirements: [
          { name: "require_review", satisfied: true },
          { name: "require_moderation", satisfied: false },
        ],
      },
    } as unknown as Partial<Bundle>);

    const insight = deriveBundleInsights("workflow", "local", bundle).find((i) =>
      i.id.endsWith("risk-tier"),
    );

    // An unsatisfied requirement means admission fails closed — the same severity class
    // as a policy composition failure (`policy:failed`), never a soft warning.
    expect(insight?.severity).toBe("critical");
    expect(insight?.detail).toContain("require_moderation");
    expect(insight?.detail).not.toContain("require_review"); // only UNSATISFIED controls named
  });

  it("escalates an unsatisfied require_declared entry (undeclared workflow under a demanding policy)", () => {
    const bundle = baseBundle({
      risk_tier: {
        declared: "safe",
        effective: "safe",
        floor: "safe",
        floor_source: "declared",
        requirements: [{ name: "require_declared", satisfied: false }],
      },
    } as unknown as Partial<Bundle>);

    const insight = deriveBundleInsights("workflow", "local", bundle).find((i) =>
      i.id.endsWith("risk-tier"),
    );

    expect(insight?.severity).toBe("critical");
    expect(insight?.detail).toContain("require_declared");
  });

  it("names cascade-only unsatisfied controls in the detail text, not just the severity (#705 Bugbot)", () => {
    // Defensive foreign-payload shape: top-level requirements all satisfied, but the
    // cascade block carries an unsatisfied control — severity is admission-blocking and
    // the MESSAGE must say so rather than claiming the tier is satisfied.
    const bundle = baseBundle({
      risk_tier: {
        declared: "safe",
        effective: "human_gated",
        floor: "safe",
        floor_source: "cascade:child_workflow",
        requirements: [{ name: "require_redaction", satisfied: true }],
        cascade: {
          lifted_by: "child_workflow",
          effective: "human_gated",
          requirements: [{ name: "require_review", satisfied: false }],
        },
      },
    } as unknown as Partial<Bundle>);
    const tier = deriveBundleInsights("workflow", "local", bundle).find((insight) =>
      insight.id.endsWith("risk-tier"),
    );
    expect(tier?.severity).toBe("critical");
    expect(tier?.detail).toMatch(/require_review/);
    expect(tier?.detail).toMatch(/fails closed/);
    expect(tier?.detail).not.toMatch(/is satisfied/);
  });

  it("escalates a prohibited cascade lift to critical and notes the lifting sub-workflow", () => {
    const bundle = baseBundle({
      risk_tier: {
        declared: "human_gated",
        effective: "human_gated",
        floor: "human_gated",
        floor_source: "declared",
        requirements: [],
        cascade: { lifted_by: "child_workflow", effective: "prohibited", requirements: [] },
      },
    } as unknown as Partial<Bundle>);

    const insight = deriveBundleInsights("workflow", "local", bundle).find((i) =>
      i.id.endsWith("risk-tier"),
    );

    expect(insight?.severity).toBe("critical");
    expect(insight?.detail).toContain("prohibited");
    expect(insight?.detail).toContain("child_workflow");
  });

  it("attributes controls to the ENFORCED tier once, with the cascade naming only the lift", () => {
    // The wire shape after the review round: the top-level posture IS the enforced
    // (cascade-lifted) one — effective human_gated, floor_source cascade:<member>, the
    // lifted tier's requirements — and the cascade block mirrors it as the explanation.
    // The message must name each control exactly once, under the enforced tier, and the
    // cascade sentence only says who lifted it.
    const bundle = baseBundle({
      risk_tier: {
        declared: "safe",
        effective: "human_gated",
        floor: "safe",
        floor_source: "cascade:child_workflow",
        requirements: [
          { name: "require_review", satisfied: false },
          { name: "constrain_providers", satisfied: false },
        ],
        cascade: {
          lifted_by: "child_workflow",
          effective: "human_gated",
          requirements: [
            { name: "require_review", satisfied: false },
            { name: "constrain_providers", satisfied: false },
          ],
        },
      },
    } as unknown as Partial<Bundle>);

    const insight = deriveBundleInsights("workflow", "local", bundle).find((i) =>
      i.id.endsWith("risk-tier"),
    );

    expect(insight?.severity).toBe("critical"); // unsatisfied = admission denial
    expect(insight?.title).toBe("Risk tier: human_gated"); // the ENFORCED tier, never the pre-lift base
    expect(insight?.detail).toContain(
      "Tier 'human_gated' requires require_review, constrain_providers — unsatisfied",
    );
    expect(insight?.detail).toContain("Lifted to 'human_gated' by sub-workflow 'child_workflow'.");
    expect(insight?.detail?.split("require_review").length).toBe(2); // named once, not re-listed by the cascade
  });
});

describe("deriveDrainInsights", () => {
  const drain = (running: Record<string, number>): DrainStatus => ({
    logical_workflow: "DemoWorkflow",
    current_workflow_type: "DemoWorkflow.abc123def456",
    query: "WorkflowType STARTS_WITH 'DemoWorkflow.' AND ExecutionStatus = 'Running'",
    running,
    total_running: Object.values(running).reduce((a, b) => a + b, 0),
    drained: Object.keys(running).every((t) => t === "DemoWorkflow.abc123def456"),
  });

  it("flags executions on old versions as version drift", () => {
    const insights = deriveDrainInsights(
      "workflow",
      "prod",
      drain({ "DemoWorkflow.abc123def456": 2, "DemoWorkflow.old456old456": 3 }),
    );

    expect(insights[0].severity).toBe("warning");
    expect(insights[0].title).toContain("3 executions on 1 old version");
    expect(insights[0].link).toBe("#/workflows/workflow/versions?env=prod");
  });

  it("reports drained and current-version-only states as ok", () => {
    expect(deriveDrainInsights("w", "local", drain({}))[0].severity).toBe("ok");
    const current = deriveDrainInsights(
      "w",
      "local",
      drain({ "DemoWorkflow.abc123def456": 4 }),
    )[0];
    expect(current.severity).toBe("ok");
    expect(current.detail).toContain("4 running executions");
  });

  it("shares its stale-type detection with drainStale", () => {
    expect(drainStale(drain({ "DemoWorkflow.abc123def456": 2, "DemoWorkflow.old": 3 }))).toEqual({
      staleTypes: ["DemoWorkflow.old"],
      staleRuns: 3,
    });
    expect(drainStale(drain({ "DemoWorkflow.abc123def456": 4 }))).toEqual({
      staleTypes: [],
      staleRuns: 0,
    });
  });
});

describe("validationIssueSourceLinks", () => {
  const project: RepoProvenance = {
    repo_url: "https://github.com/acme/flows",
    repo_sha: "abc123",
    manifest_repo_path: "typeflux.project.yaml",
  };
  const workflows = [{ id: "wf_a", path: "workflows/wf_a.yaml" }, { id: "wf_dir", path: null }];

  it("links the manifest and the expected workflow YAML for file-class failures", () => {
    const links = validationIssueSourceLinks(
      { code: "missing_workflow_file", reference: "wf_a" },
      project,
      workflows,
    );
    expect(links?.manifest).toEqual({
      blob: "https://github.com/acme/flows/blob/abc123/typeflux.project.yaml",
      history: "https://github.com/acme/flows/commits/abc123/typeflux.project.yaml",
    });
    expect(links?.workflowFile).toEqual({
      blob: "https://github.com/acme/flows/blob/abc123/workflows/wf_a.yaml",
      history: "https://github.com/acme/flows/commits/abc123/workflows/wf_a.yaml",
    });
  });

  it("covers every file-class code and is undefined for other issue classes (rendered as a dash)", () => {
    for (const code of ["missing_workflow_file", "workflow_load_error", "invalid_workflow_yaml"]) {
      expect(validationIssueSourceLinks({ code, reference: "wf_a" }, project, workflows)?.manifest).toBeDefined();
    }
    // A non-file-class code never carries source links (it points elsewhere) — undefined, so the
    // table can distinguish "no links by class" (dash) from "file-class but un-provenanced" (note).
    expect(
      validationIssueSourceLinks({ code: "policy_admission_failure", reference: "wf_a" }, project, workflows),
    ).toBeUndefined();
  });

  it("links only the manifest when the workflow is directory-declared (path unknown)", () => {
    const links = validationIssueSourceLinks(
      { code: "invalid_workflow_yaml", reference: "wf_dir" },
      project,
      workflows,
    );
    expect(links?.manifest.blob).toBeDefined();
    expect(links?.workflowFile).toBeUndefined();
  });

  it("degrades LOUDLY without git provenance: empty pairs, so SourceLinks renders the explicit note", () => {
    // File-class issue, no provenance → the affordance is present but empty ({}), which the
    // SourceLinks component renders as "source link unavailable (no repo provenance)" — the
    // app-wide loud-degradation convention, never a silently blank cell.
    const noProject = validationIssueSourceLinks(
      { code: "missing_workflow_file", reference: "wf_a" },
      undefined,
      workflows,
    );
    expect(noProject?.manifest).toEqual({});
    expect(noProject?.workflowFile).toEqual({});
    const noSha = validationIssueSourceLinks(
      { code: "missing_workflow_file", reference: "wf_a" },
      { ...project, repo_sha: null },
      workflows,
    );
    expect(noSha?.manifest).toEqual({});
  });
});
