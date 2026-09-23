import { describe, expect, it } from "vitest";

import type { DeploymentEntry, DrainStatus, RuntimePinInfo, WorkflowPromptStatus } from "./api";
import {
  deriveDrainDrift,
  deriveEnvironmentDrift,
  derivePinSkewDrift,
  derivePlanDrift,
  derivePromptDrift,
  planMismatchSeverity,
  temporalDriftOutage,
  type PinSkewResolution,
  type TemporalDriftFeed,
} from "./driftFeed";
import type { DiffEntry } from "./diff";
import type { RuntimePinCell, VersionsCell } from "./queries";

function entry(overrides: {
  planId: string;
  workflowId: string;
  environmentId: string;
  generatedAt?: string;
  ok?: boolean;
  mismatches?: Array<{ path: string; plan_value: unknown; current_value: unknown }>;
}): DeploymentEntry {
  return {
    plan_id: overrides.planId,
    plan: {
      identity: {
        workflow_id: overrides.workflowId,
        environment_id: overrides.environmentId,
        workflow_type: `${overrides.workflowId}.abc123`,
      },
      generated_at: overrides.generatedAt ?? "2026-07-01T00:00:00Z",
    },
    verification: {
      ok: overrides.ok ?? true,
      mismatches: overrides.mismatches ?? [],
    },
  } as unknown as DeploymentEntry;
}

describe("derivePlanDrift", () => {
  it("emits a critical row for identity/policy mismatches with plan → current values", () => {
    const rows = derivePlanDrift(
      [
        entry({
          planId: "p1",
          workflowId: "wf_a",
          environmentId: "prod",
          ok: false,
          mismatches: [
            { path: "identity.spec_digest", plan_value: "a".repeat(30), current_value: "b".repeat(30) },
          ],
        }),
      ],
      ["wf_a"],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].severity).toBe("critical");
    expect(rows[0].title).toContain("wf_a");
    expect(rows[0].title).toContain("prod");
    expect(rows[0].detail).toContain("identity.spec_digest");
    expect(rows[0].detail).toContain("re-approved");
    expect(rows[0].link).toBe("#/deployments");
    // No plan path on the entry → nothing to link, so no sourceLinks at all (#718).
    expect(rows[0].sourceLinks).toBeUndefined();
  });

  it("attaches plan-file source + history links when git provenance is available (#718)", () => {
    const drifted = {
      ...entry({
        planId: "p1",
        workflowId: "wf_a",
        environmentId: "prod",
        ok: false,
        mismatches: [{ path: "policy.policy_hash", plan_value: "x", current_value: "y" }],
      }),
      path: "deploy/wf_a.prod.plan.yaml",
    } as unknown as DeploymentEntry;
    const rows = derivePlanDrift([drifted], ["wf_a"], {
      repo_url: "https://github.com/acme/flows",
      repo_sha: "abc123",
      manifest_repo_path: "typeflux.project.yaml",
    });
    expect(rows[0].sourceLinks).toEqual({
      blob: "https://github.com/acme/flows/blob/abc123/deploy/wf_a.prod.plan.yaml",
      history: "https://github.com/acme/flows/commits/abc123/deploy/wf_a.prod.plan.yaml",
    });
    // The plan path IS known but the project has no resolvable sha — degrade LOUDLY to an empty
    // pair (SourceLinks then renders the "no repo provenance" note), never silent omission.
    const noProv = derivePlanDrift([drifted], ["wf_a"], { repo_url: "https://github.com/acme/flows", repo_sha: null });
    expect(noProv[0].sourceLinks).toEqual({});
  });

  it("classifies every real verification path as critical, unknown paths as warning", () => {
    // verify_deployment_plan emits exactly these four paths today.
    expect(planMismatchSeverity("identity.spec_digest")).toBe("critical");
    expect(planMismatchSeverity("identity.workflow_type")).toBe("critical");
    expect(planMismatchSeverity("policy.policy_hash")).toBe("critical");
    expect(planMismatchSeverity("preflight.ok")).toBe("critical");
    // A future path the classifier doesn't know degrades to warning, not silence.
    expect(planMismatchSeverity("deployment.image")).toBe("warning");
    const rows = derivePlanDrift(
      [
        entry({
          planId: "p1",
          workflowId: "wf_a",
          environmentId: "prod",
          ok: false,
          mismatches: [{ path: "preflight.ok", plan_value: true, current_value: false }],
        }),
      ],
      ["wf_a"],
    );
    expect(rows[0].severity).toBe("critical");
  });

  it("only considers the latest plan per (workflow, environment)", () => {
    const rows = derivePlanDrift(
      [
        entry({
          planId: "old",
          workflowId: "wf_a",
          environmentId: "prod",
          generatedAt: "2026-01-01T00:00:00Z",
          ok: false,
          mismatches: [{ path: "identity.spec_digest", plan_value: "x", current_value: "y" }],
        }),
        entry({
          planId: "new",
          workflowId: "wf_a",
          environmentId: "prod",
          generatedAt: "2026-06-01T00:00:00Z",
          ok: true,
        }),
      ],
      ["wf_a"],
    );
    expect(rows).toHaveLength(0);
  });

  it("emits a warning row per workflow with no approved plan", () => {
    const rows = derivePlanDrift([entry({ planId: "p1", workflowId: "wf_a", environmentId: "prod" })], [
      "wf_a",
      "wf_b",
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe("plan:none:wf_b");
    expect(rows[0].severity).toBe("warning");
    expect(rows[0].detail).toContain("Generate a plan");
  });

  it("ranks drifted plans above coverage gaps", () => {
    const rows = derivePlanDrift(
      [
        entry({
          planId: "p1",
          workflowId: "wf_a",
          environmentId: "prod",
          ok: false,
          mismatches: [{ path: "policy.policy_hash", plan_value: "x", current_value: "y" }],
        }),
      ],
      ["wf_a", "wf_b"],
    );
    expect(rows.map((row) => row.severity)).toEqual(["critical", "warning"]);
  });

  it("is empty for a fully covered, verified project", () => {
    expect(
      derivePlanDrift([entry({ planId: "p1", workflowId: "wf_a", environmentId: "prod" })], ["wf_a"]),
    ).toEqual([]);
  });
});

describe("deriveEnvironmentDrift", () => {
  const critical = (path: string): DiffEntry => ({
    path,
    section: path.split(".")[0] ?? "bundle",
    severity: "critical",
    kind: "changed",
    left: "a",
    right: "b",
  });
  const warning = (path: string): DiffEntry => ({ ...critical(path), severity: "warning" });

  it("keeps critical entries only and links to the pairwise diff", () => {
    const rows = deriveEnvironmentDrift("wf_a", "local", "prod", [
      critical("workflow.spec_digest"),
      warning("runtime.provider.model"),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].severity).toBe("critical");
    expect(rows[0].detail).toContain("workflow.spec_digest");
    expect(rows[0].detail).not.toContain("runtime.provider.model");
    expect(rows[0].link).toBe("#/workflows/wf_a/diff?left=local&right=prod");
  });

  it("is empty when the differences are warning/info only", () => {
    expect(deriveEnvironmentDrift("wf_a", "local", "prod", [warning("runtime.provider.model")])).toEqual(
      [],
    );
  });

  it("summarizes beyond three critical paths with a count", () => {
    const rows = deriveEnvironmentDrift("wf_a", "local", "prod", [
      critical("workflow.spec_digest"),
      critical("policy.policy_hash"),
      critical("secret_references[0].configured"),
      critical("secret_references[1].configured"),
    ]);
    expect(rows[0].detail).toContain("+1 more");
  });
});

describe("derivePromptDrift", () => {
  const status = (prompts: Array<Record<string, unknown>>): WorkflowPromptStatus =>
    ({ prompts }) as unknown as WorkflowPromptStatus;

  it("emits a warning row per drifting prompt with the version pair", () => {
    const rows = derivePromptDrift("wf_a", "local", status([
      { name: "triage", status: "drift", registry_version: 7, last_run_version: 5 },
      { name: "summarize", status: "in_sync", registry_version: 2, last_run_version: 2 },
    ]));
    expect(rows).toHaveLength(1);
    expect(rows[0].title).toContain("triage");
    expect(rows[0].title).toContain("7");
    expect(rows[0].title).toContain("5");
    expect(rows[0].link).toBe("#/workflows/wf_a?env=local&section=prompt-registry");
  });

  it("is empty when everything is in sync or unknown", () => {
    expect(
      derivePromptDrift("wf_a", "local", status([{ name: "triage", status: "in_sync" }])),
    ).toEqual([]);
    expect(derivePromptDrift("wf_a", "local", status([]))).toEqual([]);
  });
});

describe("deriveDrainDrift", () => {
  const drain = (running: Record<string, number>, current = "DemoWorkflow.cur"): DrainStatus => ({
    logical_workflow: "DemoWorkflow",
    current_workflow_type: current,
    query: "…",
    running,
    total_running: Object.values(running).reduce((a, b) => a + b, 0),
    drained: Object.keys(running).every((t) => t === current),
  });
  const cell = (overrides: Partial<VersionsCell> & { workflowId: string }): VersionsCell => ({
    pending: false,
    updatedAt: 1,
    ...overrides,
  });

  it("emits a per-workflow warning naming the old versioned types and the drain remediation", () => {
    const feed = deriveDrainDrift("prod", [
      cell({
        workflowId: "wf_a",
        drain: drain({ "DemoWorkflow.cur": 2, "DemoWorkflow.old1": 3, "DemoWorkflow.old2": 1 }),
      }),
    ]);
    expect(feed.unavailable).toEqual([]);
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0].id).toBe("drain:wf_a");
    expect(feed.rows[0].severity).toBe("warning");
    expect(feed.rows[0].title).toContain("4 executions still on 2 old versions");
    expect(feed.rows[0].detail).toContain("DemoWorkflow.old1");
    expect(feed.rows[0].detail).toContain("DemoWorkflow.old2");
    expect(feed.rows[0].detail).toContain("Drain before decommissioning");
    expect(feed.rows[0].link).toBe("#/workflows/wf_a/versions?env=prod");
  });

  it("emits no row for a workflow whose executions are all on the current version", () => {
    const feed = deriveDrainDrift("prod", [
      cell({ workflowId: "wf_a", drain: drain({ "DemoWorkflow.cur": 4 }) }),
      cell({ workflowId: "wf_b", drain: drain({}) }),
    ]);
    expect(feed.rows).toEqual([]);
    expect(feed.unavailable).toEqual([]);
  });

  it("surfaces an errored Temporal read as unavailable, never a silent all-clear", () => {
    const feed = deriveDrainDrift("prod", [
      cell({ workflowId: "wf_a", drain: drain({ "DemoWorkflow.cur": 1, "DemoWorkflow.old1": 2 }) }),
      cell({ workflowId: "wf_b", error: "temporal unreachable", drain: undefined }),
    ]);
    expect(feed.rows.map((row) => row.id)).toEqual(["drain:wf_a"]);
    expect(feed.unavailable).toEqual(["wf_b"]);
  });

  it("surfaces a fail-closed not-drained verdict with no enumerated old types as a warning, never an all-clear", () => {
    // A truncated visibility scan can fail closed: `drained: false` while `running` carries no
    // non-current key. Dropping the workflow would contradict the backend's unsafe verdict.
    const failedClosed: DrainStatus = {
      ...drain({ "DemoWorkflow.cur": 2 }),
      drained: false,
    };
    const feed = deriveDrainDrift("prod", [cell({ workflowId: "wf_a", drain: failedClosed })]);
    expect(feed.unavailable).toEqual([]);
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0].id).toBe("drain:wf_a:unsafe");
    expect(feed.rows[0].severity).toBe("warning");
    expect(feed.rows[0].title).toContain("unsafe");
    expect(feed.rows[0].detail).toContain("Do not decommission");
    expect(feed.rows[0].link).toBe("#/workflows/wf_a/versions?env=prod");
  });
});

describe("derivePinSkewDrift", () => {
  const pin = (spec: string | null): RuntimePinInfo => ({ spec_digest: spec, pinned_at: "2026-07-01T00:00:00Z" });
  const cell = (overrides: Partial<RuntimePinCell> & { workflowId: string }): RuntimePinCell => ({
    unavailable: false,
    pending: false,
    updatedAt: 1,
    ...overrides,
  });

  const resolution = (entries: Record<string, PinSkewResolution>): Map<string, PinSkewResolution> =>
    new Map(Object.entries(entries));

  it("reuses the run-inspector skew semantics: a differing pinned digest is a per-workflow row", () => {
    const feed = derivePinSkewDrift(
      "prod",
      [cell({ workflowId: "wf_a", pin: pin("a".repeat(20)) })],
      resolution({ wf_a: { specDigest: "b".repeat(20) } }),
    );
    expect(feed.unavailable).toEqual([]);
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0].id).toBe("pin-skew:wf_a:prod");
    expect(feed.rows[0].severity).toBe("warning");
    // Links to the run inspector (where the pin + repin action live).
    expect(feed.rows[0].link).toBe("#/workflows/wf_a/runs?env=prod");
  });

  it("emits no row when the pinned digest matches the current bundle (not a freshness verdict)", () => {
    const feed = derivePinSkewDrift(
      "prod",
      [cell({ workflowId: "wf_a", pin: pin("same-digest") })],
      resolution({ wf_a: { specDigest: "same-digest" } }),
    );
    expect(feed.rows).toEqual([]);
    expect(feed.unavailable).toEqual([]);
  });

  it("emits no row when there is no pin or no current digest to compare", () => {
    expect(
      derivePinSkewDrift("prod", [cell({ workflowId: "wf_a", pin: undefined })], new Map())
        .rows,
    ).toEqual([]);
    expect(
      derivePinSkewDrift("prod", [cell({ workflowId: "wf_a", pin: pin("x") })], resolution({ wf_a: {} }))
        .rows,
    ).toEqual([]);
  });

  it("surfaces an unavailable pin read as unknown, never a silent no-skew", () => {
    const feed = derivePinSkewDrift(
      "prod",
      [
        cell({ workflowId: "wf_a", pin: pin("a".repeat(20)) }),
        cell({ workflowId: "wf_b", unavailable: true }),
      ],
      resolution({
        wf_a: { specDigest: "b".repeat(20) },
        wf_b: { specDigest: "c".repeat(20) },
      }),
    );
    expect(feed.rows.map((row) => row.id)).toEqual(["pin-skew:wf_a:prod"]);
    expect(feed.unavailable).toEqual(["wf_b"]);
  });

  it("flags a pinned workflow whose RESOLUTION read errored as unavailable, never a silent no-skew", () => {
    // Pin read succeeded, bundle read failed: there is no current digest to compare against, so
    // the workflow's skew is UNKNOWN — it must not vanish into a clean "no skew" state.
    const feed = derivePinSkewDrift(
      "prod",
      [cell({ workflowId: "wf_a", pin: pin("a".repeat(20)) })],
      resolution({ wf_a: { error: "bundle resolution failed" } }),
    );
    expect(feed.rows).toEqual([]);
    expect(feed.unavailable).toEqual(["wf_a (resolution read failed — skew unknown)"]);
    // A workflow with NO pin needs no current digest — a bundle error there stays a non-event
    // (there is no pinned runtime that could be skewed).
    const noPin = derivePinSkewDrift(
      "prod",
      [cell({ workflowId: "wf_a", pin: null })],
      resolution({ wf_a: { error: "bundle resolution failed" } }),
    );
    expect(noPin.rows).toEqual([]);
    expect(noPin.unavailable).toEqual([]);
  });
});

describe("temporalDriftOutage", () => {
  const feed = (unavailable: string[]): TemporalDriftFeed => ({ rows: [], unavailable });

  it("is none when every read answered — only then may an empty feed read as all-clear", () => {
    expect(temporalDriftOutage(feed([]), 3)).toEqual({ kind: "none" });
  });

  it("is partial when some workflows were unreadable, carrying their labels", () => {
    expect(temporalDriftOutage(feed(["wf_b"]), 3)).toEqual({
      kind: "partial",
      unavailable: ["wf_b"],
    });
  });

  it("is total when nothing was readable", () => {
    expect(temporalDriftOutage(feed(["wf_a", "wf_b", "wf_c"]), 3)).toEqual({ kind: "total" });
  });
});
