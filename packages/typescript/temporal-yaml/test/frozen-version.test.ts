import { createHash } from "node:crypto";

import { describe, expect, it, vi } from "vitest";

import {
  enforceFrozenWorkflowVersion,
  loadYamlSpec,
  workflowIdentityMemo,
  workflowPlanDigest,
  workflowPlanFromSpec,
  type WorkflowListClient,
} from "../src/index.js";

const SPEC = `
project: p
name: frozen_demo
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
workflow:
  name: W
  version: v1
  input: schemas:In
  steps:
    - id: s
      activity: a
`;

const digestOf = (yaml: string) => workflowPlanDigest(workflowPlanFromSpec(loadYamlSpec(yaml)));

function fakeClient(executions: { memo?: Record<string, unknown> }[]): WorkflowListClient {
  return {
    workflow: {
      // eslint-disable-next-line @typescript-eslint/require-await
      async *list() {
        yield* executions;
      },
    },
  } as unknown as WorkflowListClient;
}

describe("workflowPlanDigest (#530)", () => {
  it("is stable for identical specs and moves on a graph edit", () => {
    expect(digestOf(SPEC)).toBe(digestOf(SPEC));
    expect(digestOf(SPEC)).toMatch(/^[0-9a-f]{64}$/);
    // The digest pins the GRAPH: renaming a step changes it, the version label doesn't.
    expect(digestOf(SPEC.replace("- id: s", "- id: s_renamed"))).not.toBe(digestOf(SPEC));
    expect(digestOf(SPEC.replace("version: v1", "version: v2"))).toBe(digestOf(SPEC));
  });
});

describe("workflowIdentityMemo (#530; Python _workflow_identity_memo)", () => {
  it("stamps digest, workflow, project, and the TS-specific version label", () => {
    const spec = loadYamlSpec(SPEC);
    expect(workflowIdentityMemo(spec, "d1")).toEqual({
      typeflux_spec_digest: "d1",
      typeflux_workflow: "W",
      typeflux_project: "p",
      typeflux_workflow_version: "v1",
    });
    const unversioned = loadYamlSpec(SPEC.replace("  version: v1\n", ""));
    expect(workflowIdentityMemo(unversioned, "d1")).not.toHaveProperty("typeflux_workflow_version");
  });
});

describe("enforceFrozenWorkflowVersion (#530; Python _enforce_frozen_version_label)", () => {
  const params = {
    workflowType: "typefluxYamlWorkflow",
    workflowName: "W",
    project: "p",
    versionLabel: "v1",
    planDigest: "digest-current",
  };
  const memo = (overrides: Record<string, unknown> = {}) => ({
    typeflux_workflow: "W",
    typeflux_project: "p",
    typeflux_workflow_version: "v1",
    typeflux_spec_digest: "digest-current",
    ...overrides,
  });

  it("passes when the most recent matching execution recorded the same digest", async () => {
    await expect(
      enforceFrozenWorkflowVersion(fakeClient([{ memo: memo() }]), params),
    ).resolves.toBeUndefined();
  });

  it("throws Python's message on a digest mismatch under the frozen label", async () => {
    const client = fakeClient([{ memo: memo({ typeflux_spec_digest: "digest-recorded" }) }]);
    await expect(enforceFrozenWorkflowVersion(client, params)).rejects.toThrow(
      /workflow\.version 'v1' is frozen to spec digest 'digest-recorded'.*'digest-current'.*assign a new workflow\.version/s,
    );
  });

  it("scans past executions of OTHER workflows/projects/labels to the first identity match", async () => {
    const client = fakeClient([
      { memo: memo({ typeflux_workflow: "Other" }) },
      // Same name + label in a DIFFERENT project: labels are project-scoped
      // (Python bakes project into the digest itself).
      { memo: memo({ typeflux_project: "other-project", typeflux_spec_digest: "x" }) },
      { memo: memo({ typeflux_workflow_version: "v2", typeflux_spec_digest: "x" }) },
      {}, // no memo at all
      { memo: memo({ typeflux_spec_digest: "digest-recorded" }) }, // first REAL match wins
      { memo: memo() }, // an older equal digest must NOT rescue the label
    ]);
    await expect(enforceFrozenWorkflowVersion(client, params)).rejects.toThrow(/digest-recorded/);
  });

  it("treats a fresh label (no match) as passing", async () => {
    await expect(
      enforceFrozenWorkflowVersion(fakeClient([{ memo: memo({ typeflux_workflow: "Other" }) }]), params),
    ).resolves.toBeUndefined();
  });

  it("a matching execution whose digest memo is malformed passes (best-effort, like Python)", async () => {
    const client = fakeClient([{ memo: memo({ typeflux_spec_digest: 42 }) }]);
    await expect(enforceFrozenWorkflowVersion(client, params)).resolves.toBeUndefined();
  });

  it("warns and skips when the client has no visibility support", async () => {
    const warn = vi.fn();
    await expect(
      enforceFrozenWorkflowVersion({ workflow: {} } as unknown as WorkflowListClient, { ...params, warn }),
    ).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("no visibility support"));
  });

  it("warns and skips when the visibility query fails", async () => {
    const warn = vi.fn();
    const client = {
      workflow: {
        // eslint-disable-next-line @typescript-eslint/require-await
        async *list(): AsyncGenerator<never> {
          throw new Error("visibility store down");
        },
      },
    } as unknown as WorkflowListClient;
    await expect(enforceFrozenWorkflowVersion(client, { ...params, warn })).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("visibility query failed"));
  });

  it("stops at the scan bound and says the label was treated as fresh", async () => {
    const warn = vi.fn();
    const others = Array.from({ length: 1500 }, () => ({ memo: memo({ typeflux_workflow: "Other" }) }));
    // A REAL match hides beyond the bound — the check must not find it, and must say so.
    const client = fakeClient([...others, { memo: memo({ typeflux_spec_digest: "digest-recorded" }) }]);
    await expect(enforceFrozenWorkflowVersion(client, { ...params, warn })).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("treating the label as fresh"));
  });

  it("queries scoped to the generic workflow type, narrowed by the configured search attribute", async () => {
    let seenQuery = "";
    const client = {
      workflow: {
        // eslint-disable-next-line @typescript-eslint/require-await
        async *list(options: { query: string }): AsyncGenerator<never> {
          seenQuery = options.query;
        },
      },
    } as unknown as WorkflowListClient;
    await enforceFrozenWorkflowVersion(client, params);
    expect(seenQuery).toBe("WorkflowType = 'typefluxYamlWorkflow'");
    await enforceFrozenWorkflowVersion(client, {
      ...params,
      searchAttribute: { name: "TypefluxWorkflow", value: "W'; DROP" },
    });
    // The value is quote-escaped — injection-safe interpolation.
    expect(seenQuery).toBe("WorkflowType = 'typefluxYamlWorkflow' AND TypefluxWorkflow = 'W''; DROP'");
  });

  it("the digest envelope pins the interpreter version, not just the plan shape", () => {
    // Same plan, raw-plan hash: must DIFFER from the enveloped digest, so a
    // future interpreter-semantics bump (PLAN_INTERPRETER_VERSION) moves every
    // digest attributably instead of silently spanning two programs.
    const plan = workflowPlanFromSpec(loadYamlSpec(SPEC));
    const raw = createHash("sha256").update(JSON.stringify(plan)).digest("hex");
    expect(workflowPlanDigest(plan)).not.toBe(raw);
  });

  it("graph-inert spec edits do not move the digest (finder review)", () => {
    // An unreferenced definition with tuned timeouts never runs — it must not
    // churn a frozen label.
    const withUnused = SPEC.replace(
      "      prompt: p/x\n",
      "      prompt: p/x\n    - name: unused\n      input: schemas:In\n      output: schemas:Out\n      prompt: p/x\n      start_to_close_timeout_seconds: 99\n",
    );
    expect(digestOf(withUnused)).toBe(digestOf(SPEC));
  });
});
