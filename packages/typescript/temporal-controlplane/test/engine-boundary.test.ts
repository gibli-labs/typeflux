import { readFileSync } from "node:fs";
import { expect, it, vi } from "vitest";
import { typefluxYamlSpec } from "@typeflux/temporal-yaml";
import { workflowTaskQueueWorkers } from "../src/workers.js";
import { temporalConnectionOptions, listWorkflowExecutions } from "../src/executions.js";
import { operateTargetFromSpec } from "../src/operations.js";

it("rejects local worker queries even with an explicit queue", async () => {
  const base = JSON.parse(readFileSync(new URL("../../../../contracts/local-runtime/golden/capability-cases.json", import.meta.url), "utf8")).base;
  const spec = typefluxYamlSpec.parse(base);
  const factory = vi.fn((): never => { throw new Error("Temporal connection reached"); });
  await expect(workflowTaskQueueWorkers(spec, {
    workflowId: "root", environmentId: "local", taskQueue: "override", clientFactory: factory,
  })).rejects.toThrow(/Temporal entry point/);
  expect(() => operateTargetFromSpec(spec)).toThrow(/Temporal entry point/);
  expect(() => temporalConnectionOptions(spec)).toThrow(/Temporal entry point/);
  await expect(listWorkflowExecutions(spec, {workflowId: "root", environmentId: "local", clientFactory: factory})).rejects.toThrow(/Temporal entry point/);
  expect(factory).not.toHaveBeenCalled();
});
