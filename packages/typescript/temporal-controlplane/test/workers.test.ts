/** Task-queue worker presence (#686): runtime-neutral describe, Python's in-band degradation. */

import { loadYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { type TaskQueueClient, workflowTaskQueueWorkers } from "../src/index.js";

const SPEC = loadYamlSpec(
  "project: p\nname: n\ntask_queue: main-queue\n" +
    "runtime:\n  temporal: { address: 'localhost:7233' }\n" +
    "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
    "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
    "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
  { env: {} },
);

const fakeClient = (count: number, seen: string[] = [], fail = false): TaskQueueClient => ({
  pollerCount: async (taskQueue: string) => {
    seen.push(taskQueue);
    if (fail) throw new Error("DescribeTaskQueue: namespace not found");
    return count;
  },
  close: async () => undefined,
});

describe("workflowTaskQueueWorkers (#686)", () => {
  it("reports the live poller count for the resolved task queue", async () => {
    const seen: string[] = [];
    const workers = await workflowTaskQueueWorkers(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () => fakeClient(3, seen),
    });
    expect(seen).toEqual(["main-queue"]);
    expect(workers).toEqual({ task_queue: "main-queue", reachable: true, workers_polling: 3 });
  });

  it("an explicit override targets that queue; an EMPTY override falls back (Python `or` truthiness)", async () => {
    const seen: string[] = [];
    const overridden = await workflowTaskQueueWorkers(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      taskQueue: "start-panel-queue",
      clientFactory: async () => fakeClient(0, seen),
    });
    expect(overridden.task_queue).toBe("start-panel-queue");
    expect(overridden.workers_polling).toBe(0);

    const empty = await workflowTaskQueueWorkers(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      taskQueue: "",
      clientFactory: async () => fakeClient(1, seen),
    });
    expect(empty.task_queue).toBe("main-queue");
    expect(seen).toEqual(["start-panel-queue", "main-queue"]);
  });

  it("degrades IN-BAND on a failed connect — reachable false with Python's detail prefix, never a 500/503", async () => {
    const unreachable = await workflowTaskQueueWorkers(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () => {
        throw new Error("Failed client connect: connection refused");
      },
    });
    expect(unreachable.reachable).toBe(false);
    expect(unreachable.workers_polling).toBe(0);
    expect(unreachable.detail).toMatch(/^could not reach Temporal: /);
  });

  it("degrades IN-BAND on a failed describe too (the connect succeeded, the RPC did not)", async () => {
    const failed = await workflowTaskQueueWorkers(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () => fakeClient(0, [], true),
    });
    expect(failed).toEqual({
      task_queue: "main-queue",
      reachable: false,
      workers_polling: 0,
      detail: "could not reach Temporal: DescribeTaskQueue: namespace not found",
    });
  });
});
