/** Run-to-trace correlation (#686): observer-driven, injected langfuse transport, exclude_none shape. */

import { loadYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { type LangfuseControlPlaneTransport, workflowRunCorrelation } from "../src/index.js";

const spec = (observability: string) =>
  loadYamlSpec(
    "project: p\nname: n\ntask_queue: q\n" +
      "runtime:\n  temporal: { address: 'localhost:7233' }\n" +
      `  observability: { type: ${observability} }\n` +
      "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
      "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
      "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
    { env: {} },
  );

const transport = (overrides: Partial<LangfuseControlPlaneTransport> = {}): LangfuseControlPlaneTransport => ({
  ping: async () => undefined,
  promptLabelVersion: async () => undefined,
  lastRunPromptVersions: async () => ({}),
  traceSummary: async () => null,
  searchEnforcementTraces: async () => [],
  ...overrides,
});

// A fake visibility client: `list` yields the scripted rows (shared by the children scan and the
// #204 provenance read — under the amended contract EVERY correlation touches the seam once).
const fakeClient = (rows: Array<Record<string, unknown>>) => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  list: async function* () {
    for (const row of rows) {
      yield row as never;
    }
  },
  close: async () => undefined,
});
const emptyClientFactory = async () => fakeClient([]) as never;

describe("workflowRunCorrelation (#686)", () => {
  it("a non-langfuse observer answers the trivial reachable shape — trace/warning OMIT (exclude_none)", async () => {
    const correlation = await workflowRunCorrelation(spec("none"), { executionId: "e-1", clientFactory: emptyClientFactory });
    expect(correlation).toEqual({ execution_id: "e-1", observer: "none", reachable: true, children: [] });
    expect(Object.hasOwn(correlation, "trace")).toBe(false);
    expect(Object.hasOwn(correlation, "warning")).toBe(false);
  });

  it("langfuse with a matching trace reports the transport's summary", async () => {
    const summary = { trace_id: "t-1", status: "ok", workflow_id: "e-2" };
    const seen: string[] = [];
    const correlation = await workflowRunCorrelation(spec("langfuse"), {
      executionId: "e-2",
      clientFactory: emptyClientFactory,
      langfuse: transport({
        traceSummary: async ({ workflowId }) => {
          seen.push(workflowId);
          return summary;
        },
      }),
    });
    expect(seen).toEqual(["e-2"]);
    expect(correlation).toEqual({ execution_id: "e-2", observer: "langfuse", reachable: true, trace: summary, children: [] });
  });

  it("langfuse with NO matching trace stays reachable with the trace omitted (Python trace=None)", async () => {
    const correlation = await workflowRunCorrelation(spec("langfuse"), {
      executionId: "e-3",
      clientFactory: emptyClientFactory,
      langfuse: transport(),
    });
    expect(correlation).toEqual({ execution_id: "e-3", observer: "langfuse", reachable: true, children: [] });
  });

  it("a transport failure degrades with Python's warning prefix and a SANITIZED class-only detail", async () => {
    const correlation = await workflowRunCorrelation(spec("langfuse"), {
      executionId: "e-4",
      clientFactory: emptyClientFactory,
      langfuse: transport({
        traceSummary: async () => {
          const error = new Error("connect refused at lf.internal:3000 token sk_live_x");
          error.name = "LangfuseRequestError";
          throw error;
        },
      }),
    });
    expect(correlation.reachable).toBe(false);
    expect(correlation.warning).toBe("observability backend unreachable: LangfuseRequestError");
    expect(correlation.warning).not.toContain("sk_live");
    expect(Object.hasOwn(correlation, "trace")).toBe(false);
  });

  it("a langfuse observer WITHOUT an injected transport degrades honestly, never fabricates reachability", async () => {
    const correlation = await workflowRunCorrelation(spec("langfuse"), { executionId: "e-5", clientFactory: emptyClientFactory });
    expect(correlation).toEqual({
      execution_id: "e-5",
      observer: "langfuse",
      reachable: false,
      warning: "observability backend unreachable: no langfuse transport configured",
      children: [],
    });
  });
});

describe("correlation children listing (#55 §9)", () => {
  // A parent spec WITH a sub-workflow reference (the children tier only scans for these).
  const parentSpec = () =>
    loadYamlSpec(
      "project: p\nname: parent\ntask_queue: q\n" +
        "runtime:\n  temporal: { address: 'localhost:7233' }\n" +
        "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
        "activities: { definitions: [] }\n" +
        "workflow:\n  name: Parent\n  input: schemas:In\n  output: schemas:In\n  steps:\n    - { id: sub, workflow: some_child }\n",
      { env: {} },
    );

  it("a spec with NO sub-workflow references answers children: [] with one provenance read, no scan", async () => {
    // The amended no-refs contract (#204 review): children stays [] with no children SCAN, but
    // correlation makes exactly ONE bounded read of the parent for its migration provenance.
    const queries: string[] = [];
    const correlation = await workflowRunCorrelation(spec("none"), {
      executionId: "e-1",
      clientFactory: async () =>
        ({
          // eslint-disable-next-line @typescript-eslint/require-await
          list: async function* (query: string) {
            queries.push(query);
          },
          close: async () => undefined,
        }) as never,
    });
    expect(correlation.children).toEqual([]);
    expect(Object.hasOwn(correlation, "migrated_from")).toBe(false);
    expect(Object.hasOwn(correlation, "warning")).toBe(false);
    // One provenance read by WorkflowId — never the children scan's WorkflowType query.
    expect(queries).toEqual(["WorkflowId = 'e-1'"]);
  });

  it("surfaces migrated_from for a NON-composed workflow via the single provenance read (#204)", async () => {
    const correlation = await workflowRunCorrelation(spec("none"), {
      executionId: "migrated-case",
      clientFactory: async () =>
        fakeClient([
          {
            workflowId: "migrated-case",
            memo: {
              typeflux_migrated_from: "old-run-9",
              typeflux_migrated_from_version: "W.v1",
            },
          },
        ]) as never,
    });
    expect(correlation.migrated_from).toEqual({ run_id: "old-run-9", version_key: "W.v1" });
    expect(correlation.children).toEqual([]);
    expect(Object.hasOwn(correlation, "warning")).toBe(false);
  });

  it("a failed provenance read degrades: migrated_from omitted + warning, never an error (#204)", async () => {
    const correlation = await workflowRunCorrelation(spec("none"), {
      executionId: "e-9",
      clientFactory: async () => {
        const error = new Error("tcp connect refused at temporal.internal:7233");
        error.name = "ConnectionError";
        throw error;
      },
    });
    expect(Object.hasOwn(correlation, "migrated_from")).toBe(false);
    // Children never needed the tier for a no-refs workflow: the honest [].
    expect(correlation.children).toEqual([]);
    expect(correlation.warning).toBe("temporal unreachable for the migration provenance: ConnectionError");
    expect(correlation.reachable).toBe(true); // the observability flag is never overloaded
  });

  it("surfaces migrated_from for a COMPOSED parent even when the parent is OUTSIDE the children scan window (#204 Bugbot)", async () => {
    // A busy namespace can push the parent past the bounded WorkflowType scan — provenance
    // must come from the DEDICATED WorkflowId read (Python describes the parent first).
    const parentRow = {
      workflowId: "p-migrated",
      type: "typefluxYamlWorkflow",
      memo: { typeflux_migrated_from: "old-run-7", typeflux_migrated_from_version: "Parent.v1" },
    };
    const childRow = {
      workflowId: "p-migrated.sub",
      type: "typefluxYamlWorkflow",
      status: { name: "RUNNING" },
      memo: { typeflux_parent_workflow_id: "p-migrated", typeflux_workflow: "Child" },
    };
    const queries: string[] = [];
    const correlation = await workflowRunCorrelation(parentSpec(), {
      executionId: "p-migrated",
      clientFactory: async () =>
        ({
          // eslint-disable-next-line @typescript-eslint/require-await
          list: async function* (query: string) {
            queries.push(query);
            // The WorkflowId query answers the parent; the children scan yields ONLY the
            // child — the parent never appears in the scan window.
            if (query.startsWith("WorkflowId")) {
              yield parentRow as never;
            } else {
              yield childRow as never;
            }
          },
          close: async () => undefined,
        }) as never,
    });
    expect(correlation.migrated_from).toEqual({ run_id: "old-run-7", version_key: "Parent.v1" });
    expect(correlation.children).toEqual([
      { workflow_id: "p-migrated.sub", workflow_name: "Child", status: "RUNNING" },
    ]);
    expect(queries[0]).toBe("WorkflowId = 'p-migrated'"); // the dedicated read runs FIRST
  });

  it("surfaces migrated_from for a COMPOSED parent via the dedicated read (#204)", async () => {
    const rows = [
      {
        workflowId: "p-migrated",
        type: "typefluxYamlWorkflow",
        memo: {
          typeflux_migrated_from: "old-run-7",
          typeflux_migrated_from_version: "Parent.v1",
        },
      },
      {
        workflowId: "p-migrated.sub",
        type: "typefluxYamlWorkflow",
        status: { name: "RUNNING" },
        memo: { typeflux_parent_workflow_id: "p-migrated", typeflux_workflow: "Child" },
      },
    ];
    const correlation = await workflowRunCorrelation(parentSpec(), {
      executionId: "p-migrated",
      clientFactory: async () => fakeClient(rows) as never,
    });
    expect(correlation.migrated_from).toEqual({ run_id: "old-run-7", version_key: "Parent.v1" });
    expect(correlation.children).toEqual([
      { workflow_id: "p-migrated.sub", workflow_name: "Child", status: "RUNNING" },
    ]);
  });

  it("lists DIRECT children by the parent-link memo, dropping unrelated rows", async () => {
    const rows = [
      {
        workflowId: "p-1.sub",
        type: "typefluxYamlWorkflow",
        status: { name: "COMPLETED" },
        startTime: new Date("2026-07-15T10:00:00Z"),
        memo: { typeflux_parent_workflow_id: "p-1", typeflux_workflow: "Child" },
      },
      {
        workflowId: "other.sub",
        type: "typefluxYamlWorkflow",
        status: { name: "RUNNING" },
        memo: { typeflux_parent_workflow_id: "someone-else" },
      },
      { workflowId: "no-memo", type: "typefluxYamlWorkflow" },
    ];
    const correlation = await workflowRunCorrelation(parentSpec(), {
      executionId: "p-1",
      clientFactory: async () => fakeClient(rows) as never,
    });
    expect(correlation.children).toEqual([
      {
        workflow_id: "p-1.sub",
        workflow_name: "Child",
        status: "COMPLETED",
        start_time: "2026-07-15T10:00:00.000Z",
      },
    ]);
  });

  it("an unreachable Temporal tier OMITS children and degrades with a warning (never a 500)", async () => {
    const correlation = await workflowRunCorrelation(parentSpec(), {
      executionId: "p-1",
      clientFactory: async () => {
        const error = new Error("tcp connect refused at temporal.internal:7233");
        error.name = "ConnectionError";
        throw error;
      },
    });
    expect(Object.hasOwn(correlation, "children")).toBe(false);
    expect(correlation.warning).toBe("temporal unreachable for the children listing: ConnectionError");
    expect(correlation.warning).not.toContain("temporal.internal");
    // The trace half is untouched: observer none stays reachable.
    expect(correlation.reachable).toBe(true);
  });
});
