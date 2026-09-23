/** The drain view for the ts-plan-argument binding (#686): memo-grouped running counts. */

import { loadYamlSpec, workflowPlanDigest, workflowPlanFromSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import {
  type ExecutionsVisibilityClient,
  ProjectControlPlaneError,
  UNIDENTIFIED_VERSION_SUFFIX,
  versionIdentityKey,
  type VisibilityExecutionInfo,
  workflowDrainStatus,
} from "../src/index.js";

const yaml = (extraTemporal = "", version = "") =>
  "project: p\nname: n\ntask_queue: q\n" +
  `runtime:\n  temporal: { address: 'localhost:7233'${extraTemporal} }\n` +
  "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
  "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
  `workflow:\n  name: W\n${version}  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n`;

const SPEC = loadYamlSpec(yaml(), { env: {} });
const CURRENT_DIGEST = workflowPlanDigest(workflowPlanFromSpec(SPEC));
const CURRENT_KEY = `W.${CURRENT_DIGEST.slice(0, 12)}`;

const row = (memo: Record<string, unknown>): VisibilityExecutionInfo => ({
  workflowId: "wf",
  type: "typefluxYamlWorkflow",
  status: { name: "RUNNING" },
  memo,
});

const fakeClient = (rows: VisibilityExecutionInfo[], seen: string[] = []): ExecutionsVisibilityClient => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  async *list(query: string) {
    seen.push(query);
    yield* rows;
  },
  close: async () => undefined,
} as unknown as ExecutionsVisibilityClient);

describe("versionIdentityKey (Python registered_workflow_type parity)", () => {
  it("renders {logical}.{label} when a label exists, else {logical}.{digest[:12]}", () => {
    expect(versionIdentityKey("W", "v2", "abcdef")).toBe("W.v2");
    expect(versionIdentityKey("W", undefined, "0123456789abcdef")).toBe("W.0123456789ab");
    // No digest either: the collision-proof fail-safe (contains a space no
    // label/digest can carry) — never equal to a current key.
    expect(versionIdentityKey("W", undefined, undefined)).toBe(`W.${UNIDENTIFIED_VERSION_SUFFIX}`);
    // Truthy checks: empty-string memo values never render an empty suffix.
    expect(versionIdentityKey("W", "", "")).toBe(`W.${UNIDENTIFIED_VERSION_SUFFIX}`);
  });
});

describe("workflowDrainStatus (#686)", () => {
  it("groups RUNNING executions by memo version identity and reports drained only on the current key", async () => {
    const rows = [
      row({ typeflux_workflow: "W", typeflux_project: "p", typeflux_spec_digest: CURRENT_DIGEST }),
      row({ typeflux_workflow: "W", typeflux_project: "p", typeflux_spec_digest: CURRENT_DIGEST }),
      // An older labeled version still running → its own key, not drained.
      row({
        typeflux_workflow: "W",
        typeflux_project: "p",
        typeflux_spec_digest: "stale-digest-000",
        typeflux_workflow_version: "v1",
      }),
      // Foreign identity under the same generic type: excluded from the counts entirely.
      row({ typeflux_workflow: "Other", typeflux_project: "p", typeflux_spec_digest: "x" }),
      row({ typeflux_workflow: "W", typeflux_project: "other", typeflux_spec_digest: "x" }),
    ];
    const queries: string[] = [];
    const drain = await workflowDrainStatus(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () => fakeClient(rows, queries),
    });
    // Running-only server-side (Python's `_drain_query` ExecutionStatus filter).
    expect(queries).toEqual(["WorkflowType = 'typefluxYamlWorkflow' AND ExecutionStatus = 'Running'"]);
    expect(drain.logical_workflow).toBe("W");
    expect(drain.current_workflow_type).toBe(CURRENT_KEY);
    // Sorted keys (Python `dict(sorted(...))`), counts per version identity.
    expect(drain.running).toEqual({ [CURRENT_KEY]: 2, "W.v1": 1 });
    expect(drain.total_running).toBe(3);
    expect(drain.drained).toBe(false);
  });

  it("is drained when only the current version runs — and vacuously with nothing running", async () => {
    const current = await workflowDrainStatus(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () =>
        fakeClient([row({ typeflux_workflow: "W", typeflux_project: "p", typeflux_spec_digest: CURRENT_DIGEST })]),
    });
    expect(current.drained).toBe(true);
    expect(current.total_running).toBe(1);

    const empty = await workflowDrainStatus(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () => fakeClient([]),
    });
    expect(empty.running).toEqual({});
    expect(empty.total_running).toBe(0);
    expect(empty.drained).toBe(true);
  });

  it("keys a labeled CURRENT version by its label (never narrowing the safety scan)", async () => {
    const labeled = loadYamlSpec(
      yaml(", workflow_search_attribute: TypefluxWorkflow", "  version: v2\n"),
      { env: {} },
    );
    const labeledDigest = workflowPlanDigest(workflowPlanFromSpec(labeled));
    const queries: string[] = [];
    const drain = await workflowDrainStatus(labeled, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () =>
        fakeClient(
          [
            row({
              typeflux_workflow: "W",
              typeflux_project: "p",
              typeflux_spec_digest: labeledDigest,
              typeflux_workflow_version: "v2",
            }),
            // A memo missing the digest AND label reads `W.unknown` — fail-safe not-drained.
            row({ typeflux_workflow: "W", typeflux_project: "p" }),
          ],
          queries,
        ),
    });
    // The SAFETY scan never narrows by the attribute (codex): pre-attribute
    // runs must stay visible.
    expect(queries).toEqual([
      "WorkflowType = 'typefluxYamlWorkflow' AND ExecutionStatus = 'Running'",
    ]);
    expect(drain.current_workflow_type).toBe("W.v2");
    expect(drain.running).toEqual({ [`W.${UNIDENTIFIED_VERSION_SUFFIX}`]: 1, "W.v2": 1 });
    expect(drain.drained).toBe(false);
  });

  it("maps a failed connect to 503 TemporalUnavailable with Python's message prefix", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await workflowDrainStatus(SPEC, {
        workflowId: "wf",
        environmentId: "local",
        clientFactory: async () => {
          throw new Error("Failed client connect: connection refused");
        },
      });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(503);
    expect(caught?.errorName).toBe("TemporalUnavailable");
    expect(caught?.message).toMatch(/^computing the drain view failed: /);
  });
});

describe("drain safety (review round)", () => {
  it("never narrows the drain query by the search attribute", async () => {
    const spec = loadYamlSpec(yaml(", workflow_search_attribute: TypefluxLogicalName"), { env: {} });
    const seen: string[] = [];
    const status = await workflowDrainStatus(spec, {
      workflowId: "workflow",
      environmentId: "local",
      clientFactory: async () => fakeClient([], seen),
    });
    // Runs started BEFORE the attribute was configured must stay visible —
    // the safety view scans the generic type only (codex).
    expect(seen[0]).not.toContain("TypefluxLogicalName");
    expect(status.query).toBe("WorkflowType = 'typefluxYamlWorkflow' AND ExecutionStatus = 'Running'");
  });

  it("a truncated scan fails CLOSED (never drained from a partial view)", async () => {
    const rows = Array.from({ length: 1001 }, () =>
      row({ typeflux_workflow: "W", typeflux_project: "p", typeflux_spec_digest: CURRENT_DIGEST }),
    );
    const status = await workflowDrainStatus(SPEC, {
      workflowId: "workflow",
      environmentId: "local",
      clientFactory: async () => fakeClient(rows),
    });
    // Every SEEN row is current, but unseen rows could hide an old version.
    expect(status.drained).toBe(false);
  });

  it("a literal workflow.version 'unknown' cannot collide with unidentified memos", async () => {
    const spec = loadYamlSpec(yaml("", "  version: unknown\n"), { env: {} });
    const rows = [
      row({ typeflux_workflow: "W", typeflux_project: "p", typeflux_workflow_version: "unknown" }),
      row({ typeflux_workflow: "W", typeflux_project: "p" }),
    ];
    const status = await workflowDrainStatus(spec, {
      workflowId: "workflow",
      environmentId: "local",
      clientFactory: async () => fakeClient(rows),
    });
    expect(status.drained).toBe(false); // the unidentified memo reads as draining
    expect(Object.keys(status.running)).toContain(`W.${UNIDENTIFIED_VERSION_SUFFIX}`);
  });
});
