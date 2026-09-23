import { describe, expect, it } from "vitest";

import { workerCreateOptions } from "../src/index.js";

const activities = { alpha: async () => ({ ok: true }) };

describe("workerCreateOptions (#450)", () => {
  it("assembles minimal options (taskQueue + activities), omitting absent fields", () => {
    const opts = workerCreateOptions({ taskQueue: "q", activities });
    expect(opts.taskQueue).toBe("q");
    expect(opts.activities).toBe(activities);
    expect("namespace" in opts).toBe(false);
    expect("workflowsPath" in opts).toBe(false);
    expect("connection" in opts).toBe(false);
  });

  it("includes namespace + workflowsPath when provided", () => {
    const opts = workerCreateOptions({
      taskQueue: "q",
      activities,
      namespace: "ns",
      workflowsPath: "/wf.js",
    });
    expect(opts.namespace).toBe("ns");
    expect(opts.workflowsPath).toBe("/wf.js");
  });

  it("merges the workerOptions passthrough; explicit taskQueue/activities win", () => {
    const opts = workerCreateOptions({
      taskQueue: "q",
      activities,
      workerOptions: { taskQueue: "OTHER", maxConcurrentActivityTaskExecutions: 5 },
    });
    expect(opts.taskQueue).toBe("q"); // explicit value overrides the passthrough
    expect(opts.maxConcurrentActivityTaskExecutions).toBe(5);
  });

  it("throws when taskQueue is missing", () => {
    expect(() => workerCreateOptions({ taskQueue: "", activities })).toThrow(/taskQueue is required/);
  });
});
