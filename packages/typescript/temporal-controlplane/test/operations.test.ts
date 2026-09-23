/**
 * The ts-plan-argument OPERATE tier (#563): memo verification + re-pin, start argument shape,
 * bounded 503s, and the receipt/status DTOs — all over a FAKE client seam (no Temporal server).
 */

import {
  loadYamlSpec,
  workflowPlanDigest,
  workflowPlanFromSpec,
  YAML_WORKFLOW_TYPE,
} from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { ProjectControlPlaneError } from "../src/errors.js";
import {
  type OperationsClient,
  type OperationsHandle,
  type StartHandleOptions,
  WorkflowOperations,
  type WorkflowDescription,
} from "../src/operations.js";

/** A workflow spec with a review lifecycle and a frozen version label (memo carries the label). */
const SPEC = loadYamlSpec(
  "project: p\nname: n\ntask_queue: tq\n" +
    "runtime:\n  temporal: { address: 'localhost:7233' }\n" +
    "  registry: { type: inline, prompts: { x: hi, y: yo } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
    "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
    "    - { name: b, input: schemas:In, output: schemas:In, prompt: y }\n" +
    "workflow:\n  name: W\n  version: v3\n  input: schemas:In\n" +
    "  lifecycle:\n    enabled: true\n    review:\n      after_step: s1\n" +
    "      user_decisions: { approve: { route: s2 }, reject: { route: s2 } }\n" +
    "  steps:\n    - { id: s1, activity: a }\n    - { id: s2, activity: b }\n",
  { env: {} },
);
const DIGEST = workflowPlanDigest(workflowPlanFromSpec(SPEC));

/** A spec with no version label (the memo omits `typeflux_workflow_version`). */
const SPEC_NO_VERSION = loadYamlSpec(
  "project: p\nname: n\ntask_queue: tq\n" +
    "runtime:\n  temporal: { address: 'localhost:7233' }\n" +
    "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
    "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
    "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
  { env: {} },
);
const DIGEST_NV = workflowPlanDigest(workflowPlanFromSpec(SPEC_NO_VERSION));
const DIGEST_NV_SLICE = DIGEST_NV.slice(0, 12);

const goodMemo = (): Record<string, unknown> => ({
  typeflux_project: "p",
  typeflux_workflow: "W",
  typeflux_workflow_version: "v3",
  typeflux_spec_digest: DIGEST,
});

interface FakeCalls {
  getHandle: Array<{ workflowId: string; runId: string | undefined }>;
  starts: StartHandleOptions[];
  queries: string[];
  signals: Array<{ type: string; args: unknown[] }>;
}

/** A fake operations client: canned `describe`, recorded start/query/signal. */
function fakeClient(
  description: WorkflowDescription | (() => Promise<WorkflowDescription>),
  options: { startRunId?: string; status?: unknown } = {},
): { client: OperationsClient; calls: FakeCalls } {
  const calls: FakeCalls = { getHandle: [], starts: [], queries: [], signals: [] };
  // Each getHandle() returns a DISTINCT handle tagged with its runId, and query/signal record
  // the tag — so the tests can prove the RE-PINNED handle (not the stale unpinned one) is the
  // one actually queried/signaled (the TOCTOU property, not just call counts).
  const makeHandle = (runId: string | undefined): OperationsHandle => ({
    describe: () => (typeof description === "function" ? description() : Promise.resolve(description)),
    query: async <Ret>(queryType: string) => {
      calls.queries.push(`${queryType}@${runId ?? "<unpinned>"}`);
      return (options.status ?? {}) as Ret;
    },
    signal: async (signalType, ...args) => {
      calls.signals.push({ type: `${signalType}@${runId ?? "<unpinned>"}`, args });
    },
  });
  const client: OperationsClient = {
    getHandle: (workflowId, runId) => {
      calls.getHandle.push({ workflowId, runId });
      return makeHandle(runId);
    },
    start: async (opts) => {
      calls.starts.push(opts);
      return { runId: options.startRunId };
    },
    close: async () => undefined,
  };
  return { client, calls };
}

describe("WorkflowOperations.start — plan-as-argument shape (#563)", () => {
  it("dispatches [plan, input] with the identity memo, spec task queue, and receipt", async () => {
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE }, { startRunId: "run-xyz" });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    const receipt = await ops.start({ claims: [] }, { workflowId: "exec-1" });

    expect(calls.starts).toHaveLength(1);
    const start = calls.starts[0]!;
    expect(start.workflowType).toBe(YAML_WORKFLOW_TYPE);
    expect(start.taskQueue).toBe("tq");
    expect(start.workflowId).toBe("exec-1");
    // Exactly two positional args, order fixed: the plan first, the input second.
    expect(start.args).toHaveLength(2);
    expect(start.args[1]).toEqual({ claims: [] });
    expect((start.args[0] as { steps: unknown[] }).steps).toBeDefined();
    // The identity memo carries digest + names + the version label.
    expect(start.memo).toEqual({
      typeflux_spec_digest: DIGEST,
      typeflux_workflow: "W",
      typeflux_project: "p",
      typeflux_workflow_version: "v3",
    });
    expect(receipt).toEqual({
      workflow_id: "exec-1",
      run_id: "run-xyz",
      workflow_name: "W",
      workflow_type: YAML_WORKFLOW_TYPE,
      spec_digest: DIGEST,
      task_queue: "tq",
      trace_query_hint: { workflow_id: "exec-1", limit: 1 },
    });
  });

  it("an EMPTY-string task_queue override falls back to the spec queue (Python truthiness, not ??)", async () => {
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    const receipt = await ops.start({ claims: [] }, { workflowId: "e", taskQueue: "" });
    expect(calls.starts[0]!.taskQueue).toBe("tq");
    expect(receipt.task_queue).toBe("tq");
  });

  it("a non-empty task_queue override is honored", async () => {
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await ops.start({ claims: [] }, { workflowId: "e", taskQueue: "other-q" });
    expect(calls.starts[0]!.taskQueue).toBe("other-q");
  });

  it("passes the configured search attribute (name → logical workflow name)", async () => {
    const attrSpec = loadYamlSpec(
      "project: p\nname: n\ntask_queue: tq\n" +
        "runtime:\n  temporal: { address: 'localhost:7233', workflow_search_attribute: TypefluxWorkflow }\n" +
        "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
        "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
      { env: {} },
    );
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE });
    const ops = new WorkflowOperations(attrSpec, { clientFactory: async () => client });
    await ops.start({}, { workflowId: "e" });
    expect(calls.starts[0]!.searchAttribute).toEqual({ name: "TypefluxWorkflow", value: "W" });
  });

  it("maps a connect failure at start to 503 TemporalUnavailable", async () => {
    const ops = new WorkflowOperations(SPEC, {
      clientFactory: async () => {
        throw new Error("Failed to connect to Temporal: connection refused");
      },
    });
    await expect(ops.start({}, { workflowId: "e" })).rejects.toMatchObject({ status: 503 });
  });
});

describe("WorkflowOperations.boundHandle — memo verification + re-pin (#563)", () => {
  const run = async (
    description: WorkflowDescription,
    spec = SPEC,
  ): Promise<{ status: unknown; calls: FakeCalls }> => {
    const { client, calls } = fakeClient(description, { status: { state: "running" } });
    const ops = new WorkflowOperations(spec, { clientFactory: async () => client });
    const result = await ops.status("exec-1");
    return { status: result.status, calls };
  };

  it("wrong workflow type → 409 LifecycleBindingError", async () => {
    const { client } = fakeClient({ type: "someOtherWorkflow", memo: goodMemo() });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await expect(ops.status("exec-1")).rejects.toMatchObject({
      status: 409,
      errorName: "LifecycleBindingError",
    });
  });

  it("wrong project memo → 409 LifecycleBindingError", async () => {
    const memo = { ...goodMemo(), typeflux_project: "other" };
    const { client } = fakeClient({ type: YAML_WORKFLOW_TYPE, memo });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    let caught: ProjectControlPlaneError | undefined;
    try {
      await ops.status("exec-1");
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(409);
    expect(caught?.errorName).toBe("LifecycleBindingError");
    expect(caught?.message).toMatch(/belongs to project 'other', not the bound project 'p'/);
  });

  it("wrong workflow memo → 409 LifecycleBindingError", async () => {
    const memo = { ...goodMemo(), typeflux_workflow: "Other" };
    const { client } = fakeClient({ type: YAML_WORKFLOW_TYPE, memo });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await expect(ops.status("exec-1")).rejects.toMatchObject({
      status: 409,
      errorName: "LifecycleBindingError",
    });
  });

  it("wrong version label memo → 409 LifecycleBindingError (only when a label is declared)", async () => {
    const memo = { ...goodMemo(), typeflux_workflow_version: "v2" };
    const { client } = fakeClient({ type: YAML_WORKFLOW_TYPE, memo });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    let caught: ProjectControlPlaneError | undefined;
    try {
      await ops.status("exec-1");
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(409);
    expect(caught?.message).toMatch(/carries version 'v2', not the routed version 'v3'/);
  });

  it("no declared version → the version memo is NOT checked (verification passes)", async () => {
    // The spec has no version label; an execution memo lacking the version key still binds.
    const memo = { typeflux_project: "p", typeflux_workflow: "W" };
    const { status } = await run({ type: YAML_WORKFLOW_TYPE, runId: "r", memo }, SPEC_NO_VERSION);
    // A legacy (pre-slice-4) status is normalized to the always-present-[] convention (item 2).
    expect(status).toEqual({ state: "running", waiting_gates: [] });
  });

  it("re-pins to the described run id when the handle was id-only (TOCTOU guard)", async () => {
    const { calls } = await run({ type: YAML_WORKFLOW_TYPE, runId: "pinned-run", memo: goodMemo() });
    // First getHandle is id-only (runId undefined), then a re-pin to the described run id.
    expect(calls.getHandle).toEqual([
      { workflowId: "exec-1", runId: undefined },
      { workflowId: "exec-1", runId: "pinned-run" },
    ]);
    // The @tag PROVES the query went through the RE-PINNED handle, not the stale one.
    expect(calls.queries).toEqual(["typeflux_lifecycle_status@pinned-run"]);
  });

  it("does NOT re-pin when the caller already addressed a run id", async () => {
    const { client, calls } = fakeClient(
      { type: YAML_WORKFLOW_TYPE, runId: "pinned-run", memo: goodMemo() },
      { status: {} },
    );
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await ops.status("exec-1", { runId: "caller-run" });
    expect(calls.getHandle).toEqual([{ workflowId: "exec-1", runId: "caller-run" }]);
  });
});

describe("WorkflowOperations.migrate — terminate-and-resubmit (#204)", () => {
  const NEW_KEY = `W.${DIGEST_NV_SLICE}`;
  const oldMemo = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
    typeflux_project: "p",
    typeflux_workflow: "W",
    // A DIFFERENT digest than SPEC_NO_VERSION's, so the version identity differs (migrate target).
    typeflux_spec_digest: "0".repeat(64),
    ...overrides,
  });

  interface MigrateCalls {
    getHandle: Array<{ workflowId: string; runId: string | undefined }>;
    starts: StartHandleOptions[];
    terminates: Array<{ reason: string | undefined; runId: string | undefined }>;
    pollerCounts: string[];
    log: string[];
  }

  function fakeMigrateClient(
    description: WorkflowDescription,
    options: {
      pollers?: number;
      status?: unknown;
      startInput?: unknown;
      startRunId?: string;
      terminateError?: Error;
      startError?: Error;
      listMemos?: Array<Record<string, unknown>>;
    } = {},
  ): { client: OperationsClient; calls: MigrateCalls } {
    const calls: MigrateCalls = { getHandle: [], starts: [], terminates: [], pollerCounts: [], log: [] };
    const makeHandle = (runId: string | undefined): OperationsHandle => ({
      describe: () => Promise.resolve(description),
      query: async <Ret>() => (options.status ?? { state: "running" }) as Ret,
      signal: async () => undefined,
      terminate: async (reason) => {
        if (options.terminateError !== undefined) throw options.terminateError;
        calls.terminates.push({ reason, runId });
        calls.log.push("terminate");
      },
      fetchStartInput: async () => options.startInput ?? { claims: [] },
    });
    const client: OperationsClient = {
      getHandle: (workflowId, runId) => {
        calls.getHandle.push({ workflowId, runId });
        return makeHandle(runId);
      },
      start: async (opts) => {
        if (options.startError !== undefined) throw options.startError;
        calls.starts.push(opts);
        calls.log.push("start");
        return { runId: options.startRunId };
      },
      pollerCount: async (taskQueue) => {
        calls.pollerCounts.push(taskQueue);
        return options.pollers ?? 1;
      },
      // Visibility support only when the test scripts it (the frozen preflight
      // degrades warn-and-skip without it, like the TS edition).
      ...(options.listMemos !== undefined
        ? {
            list: (_query: string) => {
              const rows = options.listMemos!.map((memo) => ({ memo }));
              return (async function* () {
                yield* rows;
              })();
            },
          }
        : {}),
      close: async () => undefined,
    };
    return { client, calls };
  }

  it("terminates the old run and resubmits with carried input + provenance memo", async () => {
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "old-run", memo: oldMemo() },
      { pollers: 2, startInput: { claims: [7] }, startRunId: "new-run" },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    const result = await ops.migrate("exec-1", { reason: "budget cut" });

    // Fail-closed order: terminate BEFORE start (never the reverse).
    expect(calls.log).toEqual(["terminate", "start"]);
    // Terminated on the RE-PINNED run with the canonical reason + operator note.
    expect(calls.terminates).toEqual([{ reason: `typeflux migrate to ${NEW_KEY}: budget cut`, runId: "old-run" }]);
    // The new run carries the CARRIED input as arg[1] and the provenance memo (additive).
    expect(calls.starts[0]!.args[1]).toEqual({ claims: [7] });
    expect(calls.starts[0]!.memo).toMatchObject({
      typeflux_migrated_from: "old-run",
      typeflux_migrated_from_version: "W.000000000000",
      typeflux_workflow: "W",
      typeflux_project: "p",
    });
    expect(result).toEqual({
      execution_id: "exec-1",
      old_run_id: "old-run",
      new_run_id: "new-run",
      old_version_key: "W.000000000000",
      new_version_key: NEW_KEY,
      abandoned_gate_ids: [],
      dry_run: false,
    });
  });

  it("dry_run runs every preflight and returns the preview without terminating (#791)", async () => {
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "old-run", memo: oldMemo() },
      { pollers: 2, startInput: { claims: [7] }, startRunId: "new-run" },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    const result = await ops.migrate("exec-1", { dryRun: true,  reason: "budget cut" });

    // Preview (#791): every preflight ran; NOTHING was terminated or started.
    expect(calls.log).toEqual([]);
    expect(calls.terminates).toEqual([]);
    expect(calls.starts).toEqual([]);
    expect(result).toEqual({
      execution_id: "exec-1",
      old_run_id: "old-run",
      new_run_id: null,
      old_version_key: "W.000000000000",
      new_version_key: NEW_KEY,
      abandoned_gate_ids: [],
      dry_run: true,
    });
  });

  it("refuses a same-version migrate (422) and never terminates", async () => {
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: oldMemo({ typeflux_spec_digest: DIGEST_NV }) },
      { pollers: 1 },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    await expect(ops.migrate("exec-1", {})).rejects.toMatchObject({ status: 422 });
    expect(calls.terminates).toEqual([]);
    expect(calls.starts).toEqual([]);
  });

  it("refuses (422, fail-closed) when no workers poll the target queue — no terminate", async () => {
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: oldMemo() },
      { pollers: 0 },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    await expect(ops.migrate("exec-1", {})).rejects.toMatchObject({ status: 422 });
    expect(calls.pollerCounts).toEqual(["tq"]);
    expect(calls.terminates).toEqual([]);
    expect(calls.starts).toEqual([]);
  });

  it("refuses a waiting-gate migrate unless abandon_gates; then abandons and records the gate ids", async () => {
    const gated = {
      status: { state: "waiting_for_review", waiting_gates: [{ gate_id: "g1", after_step: "s1", valid_user_decisions: {} }] },
    };
    const refuse = fakeMigrateClient({ type: YAML_WORKFLOW_TYPE, runId: "r", memo: oldMemo() }, { pollers: 1, ...gated });
    const opsRefuse = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => refuse.client });
    await expect(opsRefuse.migrate("exec-1", {})).rejects.toMatchObject({ status: 422 });
    expect(refuse.calls.terminates).toEqual([]);

    const allow = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: oldMemo() },
      { pollers: 1, startRunId: "new-run", ...gated },
    );
    const opsAllow = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => allow.client });
    const result = await opsAllow.migrate("exec-1", { abandonGates: true });
    expect(result.abandoned_gate_ids).toEqual(["g1"]);
    expect(allow.calls.terminates).toHaveLength(1);
  });

  it("refuses (409) when the old execution is a different project/workflow", async () => {
    const { client } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: oldMemo({ typeflux_project: "other" }) },
      { pollers: 1 },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    await expect(ops.migrate("exec-1", {})).rejects.toMatchObject({
      status: 409,
      errorName: "LifecycleBindingError",
    });
  });

  it("validates the carried input BEFORE terminating (422, old run stays alive; #204 review)", async () => {
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: oldMemo() },
      { pollers: 1, startInput: { claims: "not-an-array" } },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    await expect(
      ops.migrate("exec-1", {
        validateCarriedInput: () => {
          throw new ProjectControlPlaneError("migrate refused: the carried-over input is not valid", 422);
        },
      }),
    ).rejects.toMatchObject({ status: 422 });
    expect(calls.terminates).toEqual([]);
    expect(calls.starts).toEqual([]);
  });

  it("runs the frozen-version gate as a PREFLIGHT — refuses before terminating (#204 review)", async () => {
    // SPEC declares version v3; a prior execution froze v3 to ANOTHER digest.
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: { typeflux_project: "p", typeflux_workflow: "W", typeflux_spec_digest: "0".repeat(64) } },
      {
        pollers: 1,
        listMemos: [
          {
            typeflux_workflow: "W",
            typeflux_project: "p",
            typeflux_workflow_version: "v3",
            typeflux_spec_digest: "SOME-OTHER-digest",
          },
        ],
      },
    );
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    let caught: ProjectControlPlaneError | undefined;
    try {
      await ops.migrate("exec-1", {});
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toMatch(/is frozen to spec digest/);
    expect(calls.terminates).toEqual([]);
    expect(calls.starts).toEqual([]);
  });

  it("a failed replacement start is the DISTINGUISHED 500 MigratePartialError (#204 review)", async () => {
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "old-run", memo: oldMemo() },
      { pollers: 1, startError: new Error("connect reset") },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    let caught: ProjectControlPlaneError | undefined;
    try {
      await ops.migrate("exec-1", {});
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(500);
    expect(caught?.errorName).toBe("MigratePartialError");
    expect(caught?.message).toMatch(/old run old-run of execution 'exec-1' was already terminated/);
    expect(caught?.message).toMatch(/resubmit via a normal start/);
    expect(calls.terminates).toHaveLength(1); // the terminate DID happen (partial state)
  });

  it("classifies a terminate against an already-closed execution as a 409 conflict (#204 review)", async () => {
    const notFound = new Error("workflow execution already completed");
    notFound.name = "WorkflowNotFoundError";
    const { client, calls } = fakeMigrateClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: oldMemo() },
      { pollers: 1, terminateError: notFound },
    );
    const ops = new WorkflowOperations(SPEC_NO_VERSION, { clientFactory: async () => client });
    let caught: ProjectControlPlaneError | undefined;
    try {
      await ops.migrate("exec-1", {});
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(409);
    expect(caught?.errorName).toBe("MigrateExecutionClosedError");
    expect(caught?.message).toMatch(/already closed — it may have completed or been migrated concurrently/);
    expect(calls.starts).toEqual([]); // nothing was started
  });
});

describe("WorkflowOperations.status/review/cancel (#563)", () => {
  it("status returns the lifecycle snapshot + sorted valid decisions + null runtime_pin", async () => {
    const { client } = fakeClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: goodMemo() },
      { status: { state: "waiting_for_review" } },
    );
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    const result = await ops.status("exec-1");
    expect(result).toEqual({
      workflow_id: "exec-1",
      run_id: null,
      // waiting_gates normalized to [] (a pre-slice-4 worker reports none; item 2).
      status: { state: "waiting_for_review", waiting_gates: [] },
      valid_user_decisions: { approve: "s2", reject: "s2" },
      recommended_poll_interval_seconds: 1,
      runtime_pin: null,
    });
  });

  it("prefers the execution-reported waiting-gate decisions over the resolved spec (#55 §6)", async () => {
    // A drifted execution reports its OWN open gate + decisions; the CP surfaces those, not the
    // resolved spec's (approve/reject → s2). Union across waiting gates, sorted.
    const { client } = fakeClient(
      { type: YAML_WORKFLOW_TYPE, runId: "r", memo: goodMemo() },
      {
        status: {
          state: "waiting_for_review",
          waiting_gates: [
            { gate_id: "final", after_step: "b", valid_user_decisions: { release: "finalize" } },
          ],
        },
      },
    );
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    const result = await ops.status("exec-1");
    expect(result.valid_user_decisions).toEqual({ release: "finalize" });
  });

  it("submitReview signals typeflux_submit_review with the command", async () => {
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE, runId: "run-1", memo: goodMemo() });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await ops.submitReview("exec-1", { user_decision: "approve", reviewer: "alice" });
    expect(calls.signals).toEqual([
      { type: "typeflux_submit_review@run-1", args: [{ user_decision: "approve", reviewer: "alice" }] },
    ]);
  });

  it("requestCancel signals typeflux_request_cancel with the reason (null when absent)", async () => {
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE, runId: "run-1", memo: goodMemo() });
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await ops.requestCancel("exec-1", "too slow");
    await ops.requestCancel("exec-1", undefined);
    expect(calls.signals).toEqual([
      { type: "typeflux_request_cancel@run-1", args: ["too slow"] },
      { type: "typeflux_request_cancel@run-1", args: [null] },
    ]);
  });

  it("start refuses a frozen label reused for a CHANGED graph — 422 (#662)", async () => {
    const { client } = fakeClient({ type: YAML_WORKFLOW_TYPE }, { startRunId: "r" });
    // Visibility reports the label's most recent start with a DIFFERENT digest.
    (client as { list?: unknown }).list = async function* () {
      yield { memo: { ...goodMemo(), typeflux_spec_digest: "someone-elses-digest" } };
    };
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await expect(ops.start({}, { workflowId: "frozen-1" })).rejects.toMatchObject({
      status: 422,
    });
    await expect(ops.start({}, { workflowId: "frozen-1" })).rejects.toThrow(/is frozen to spec digest/);
  });

  it("start proceeds when the frozen label's recorded digest matches (#662)", async () => {
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE }, { startRunId: "r" });
    (client as { list?: unknown }).list = async function* () {
      yield { memo: goodMemo() };
    };
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    const receipt = await ops.start({}, { workflowId: "frozen-ok" });
    expect(receipt.run_id).toBe("r");
    expect(calls.starts).toHaveLength(1);
  });

  it("start degrades warn-and-skip when the client has no visibility (#662)", async () => {
    const { client, calls } = fakeClient({ type: YAML_WORKFLOW_TYPE }, { startRunId: "r" });
    // fakeClient exposes no list() — the SDK-parity best-effort path must not block starts.
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await ops.start({}, { workflowId: "no-visibility" });
    expect(calls.starts).toHaveLength(1);
  });

  it("a duplicate execution id (already-started) propagates unmapped — Python parity, never a 409", async () => {
    // Python's start lets temporalio's WorkflowExecutionAlreadyStartedError surface as the
    // generic 500-class failure; 409 is RESERVED for LifecycleBindingError. Pin that a
    // well-intentioned future mapping doesn't silently diverge.
    const already = Object.assign(new Error("Workflow execution already started"), {
      name: "WorkflowExecutionAlreadyStartedError",
    });
    const client: OperationsClient = {
      getHandle: () => ({ describe: async () => ({ type: YAML_WORKFLOW_TYPE }), query: async () => ({}) as never, signal: async () => undefined }),
      start: async () => {
        throw already;
      },
      close: async () => undefined,
    };
    const ops = new WorkflowOperations(SPEC, { clientFactory: async () => client });
    await expect(ops.start({}, { workflowId: "dup" })).rejects.toMatchObject({
      name: "WorkflowExecutionAlreadyStartedError",
    });
    await expect(ops.start({}, { workflowId: "dup" })).rejects.not.toMatchObject({ status: 409 });
  });

  it("a gRPC UNAVAILABLE on describe maps to 503 (top-level .code)", async () => {
    const err = Object.assign(new Error("14 UNAVAILABLE: connection refused"), { code: 14 });
    const ops = new WorkflowOperations(SPEC, {
      clientFactory: async () =>
        fakeClient(() => Promise.reject(err)).client,
    });
    await expect(ops.status("exec-1")).rejects.toMatchObject({ status: 503 });
  });

  it("a mid-life gRPC outage wrapped as ServiceError(cause) maps to 503 (real @temporalio shape)", async () => {
    // @temporalio/client wraps a mid-life UNAVAILABLE/DEADLINE_EXCEEDED as ServiceError(msg,
    // { cause: grpcErr }) — the real status code is on error.cause.code, NOT the top level. The
    // 503 mapping must walk the cause chain, or a cluster outage during describe/query/signal
    // would leak as a 500.
    const grpc = Object.assign(new Error("14 UNAVAILABLE: upstream connect error"), { code: 14 });
    const wrapped = Object.assign(new Error("Failed to invoke DescribeWorkflowExecution"), { cause: grpc });
    const ops = new WorkflowOperations(SPEC, {
      clientFactory: async () => fakeClient(() => Promise.reject(wrapped)).client,
    });
    await expect(ops.status("exec-1")).rejects.toMatchObject({ status: 503 });
  });

  it("a DEADLINE_EXCEEDED wrapped in cause also maps to 503", async () => {
    const grpc = Object.assign(new Error("4 DEADLINE_EXCEEDED"), { code: 4 });
    const wrapped = Object.assign(new Error("call failed"), { cause: grpc });
    const ops = new WorkflowOperations(SPEC, {
      clientFactory: async () => fakeClient(() => Promise.reject(wrapped)).client,
    });
    await expect(ops.status("exec-1")).rejects.toMatchObject({ status: 503 });
  });

  it("a NOT_FOUND-style describe error is NOT 503 — it propagates (Python parity → generic 500)", async () => {
    // Python does not map NOT_FOUND to 503/404; it propagates to the generic 500. Assert it is
    // NOT converted to a 503 ProjectControlPlaneError.
    const err = Object.assign(new Error("5 NOT_FOUND: workflow not found"), { code: 5 });
    const ops = new WorkflowOperations(SPEC, {
      clientFactory: async () => fakeClient(() => Promise.reject(err)).client,
    });
    let caught: unknown;
    try {
      await ops.status("exec-1");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBe(err);
    expect(caught).not.toBeInstanceOf(ProjectControlPlaneError);
  });
});
