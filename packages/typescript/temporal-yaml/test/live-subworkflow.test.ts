import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

import { buildRuntime, loadYamlSpec, projectSubworkflowResolver } from "../src/index.js";

// Sub-workflow live proof (#55 slice 3). Runs a PARENT YAML workflow that invokes a sibling
// CHILD workflow as a Temporal child — both a plain `workflow:` step and a `map.workflow`
// fan-out — end to end against a local dev server:
//   temporal server start-dev
//   pnpm -r build
//   TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-yaml test live-subworkflow
const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";

/** The child activity returns a schema-valid Assessment for any item. */
class AssessProvider implements ModelProvider {
  structuredCall(_params: StructuredCallParams): unknown {
    return { ok: true };
  }
}

const TASK_QUEUE = "typeflux-live-subwf";

/** The reusable CHILD workflow (Item -> Assessment), one activity. */
const CHILD = `
project: claims
name: claim_assessment
task_queue: ${TASK_QUEUE}
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      claims/assess: assess the item
  provider: { type: openai }
activities:
  definitions:
    - name: assess_item
      input: schemas:Item
      output: schemas:Assessment
      prompt: claims/assess
workflow:
  name: ClaimAssessment
  input: schemas:Item
  output: schemas:Assessment
  steps:
    - id: assess
      activity: assess_item
`;

/** A parent that invokes the child once as a plain `workflow:` step. */
const PARENT_PLAIN = `
project: claims
name: intake
task_queue: ${TASK_QUEUE}
runtime:
  temporal:
    workflow_search_attribute: TypefluxWorkflowName
  registry: { type: inline, prompts: { claims/assess: assess the item } }
  provider: { type: openai }
activities:
  definitions: []
workflow:
  name: Intake
  input: schemas:Item
  output: schemas:Assessment
  steps:
    - id: assess_claim
      workflow: claim_assessment
`;

/** A parent that fans the child over `input.items` and collects. */
const PARENT_MAP = `
project: claims
name: batch_intake
task_queue: ${TASK_QUEUE}
runtime:
  temporal: {}
  registry: { type: inline, prompts: { claims/assess: assess the item } }
  provider: { type: openai }
activities:
  definitions: []
workflow:
  name: BatchIntake
  input: schemas:ItemBatch
  output: schemas:AssessmentBatch
  steps:
    - id: assess_all
      map:
        workflow: claim_assessment
        over: input.items
        concurrency: 3
        collect: { output: schemas:AssessmentBatch, field: assessments }
`;

const schemas = {
  "schemas:Item": z.object({ id: z.string() }),
  "schemas:Assessment": z.object({ ok: z.boolean() }),
  "schemas:ItemBatch": z.object({ items: z.array(z.object({ id: z.string() })) }),
  "schemas:AssessmentBatch": z.object({ assessments: z.array(z.object({ ok: z.boolean() })) }),
};

const subworkflowsFor = (selfId: string) =>
  projectSubworkflowResolver(selfId, (id) => (id === "claim_assessment" ? loadYamlSpec(CHILD) : undefined));

describe.skipIf(!LIVE)("live sub-workflow runtime e2e (#55 slice 3)", () => {
  it("runs a parent -> child plain step; the child id is {parent}.{step} with the parent memo key", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(loadYamlSpec(PARENT_PLAIN), {
        provider: new AssessProvider(),
        schemas,
        subworkflows: subworkflowsFor("intake"),
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const parentId = `yaml-subwf-plain-${Date.now()}`;

      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { id: "claim-1" }, { workflowId: parentId }),
      );
      expect(result).toEqual({ ok: true });

      // The child ran under the deterministic id {parent}.{step_id} (#55 §6).
      const childId = `${parentId}.assess_claim`;
      const child = await client.workflow.getHandle(childId).describe();
      expect(child.memo?.["typeflux_parent_workflow_id"]).toBe(parentId);
      // The child carries its OWN identity, not the parent's.
      expect(child.memo?.["typeflux_workflow"]).toBe("ClaimAssessment");
      expect(child.memo?.["typeflux_project"]).toBe("claims");
      // Its own logical name is stamped into the configured search attribute (not the parent's).
      expect(child.searchAttributes?.["TypefluxWorkflowName"]).toEqual(["ClaimAssessment"]);
    } finally {
      await connection.close();
    }
  });

  it("fans the child over items; child ids are {parent}.{step}-{index} and collect merges results", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(loadYamlSpec(PARENT_MAP), {
        provider: new AssessProvider(),
        schemas,
        subworkflows: subworkflowsFor("batch_intake"),
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const parentId = `yaml-subwf-map-${Date.now()}`;

      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { items: [{ id: "a" }, { id: "b" }] }, { workflowId: parentId }),
      );
      expect(result).toEqual({ assessments: [{ ok: true }, { ok: true }] });

      // Each fanned child ran under {parent}.{step_id}-{index} with the parent memo key.
      for (const index of [0, 1]) {
        const child = await client.workflow.getHandle(`${parentId}.assess_all-${index}`).describe();
        expect(child.memo?.["typeflux_parent_workflow_id"]).toBe(parentId);
        expect(child.memo?.["typeflux_workflow"]).toBe("ClaimAssessment");
      }
    } finally {
      await connection.close();
    }
  });
});

describe.skipIf(!LIVE)("ALLOW_DUPLICATE still-running collision (#55 §6, design §10)", () => {
  it("a STILL-RUNNING execution occupying the child id fails the parent loudly (no orphan adoption)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(loadYamlSpec(PARENT_PLAIN), {
        provider: new AssessProvider(),
        schemas,
        subworkflows: subworkflowsFor("intake"),
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const parentId = `yaml-subwf-collision-${Date.now()}`;
      const childId = `${parentId}.assess_claim`;

      // Occupy the deterministic child id with a DETERMINISTICALLY still-running blocker:
      // the generic interpreter type started on a task queue NO worker serves, so it can
      // never progress or close — the id conflict is namespace-wide regardless of queue.
      const { workflowPlanFromSpec } = await import("../src/build-workflow.js");
      const blockerPlan = workflowPlanFromSpec(loadYamlSpec(CHILD));
      const blocker = await client.workflow.start("typefluxYamlWorkflow", {
        taskQueue: `${TASK_QUEUE}-no-worker`,
        workflowId: childId,
        args: [blockerPlan, { id: "blocker" }],
      });

      try {
        await expect(
          runtime.worker.runUntil(
            runtime.runWorkflow(client, { id: "claim-1" }, { workflowId: parentId }),
          ),
        ).rejects.toThrow(/[Cc]hild|[Aa]lready|[Ss]tarted|failed/);
        // The blocker is still the execution under the child id — the parent did NOT
        // silently adopt it (its run id is unchanged and it is still running).
        const description = await client.workflow.getHandle(childId).describe();
        expect(description.runId).toBe(blocker.firstExecutionRunId);
        expect(description.status.name).toBe("RUNNING");
      } finally {
        await client.workflow.getHandle(childId).terminate("test cleanup").catch(() => undefined);
      }
    } finally {
      // On the parent-FAILURE path the worker can still hold its connection reference for a
      // beat after runUntil rethrows — retry the close instead of failing the assertion run.
      for (let attempt = 0; attempt < 20; attempt += 1) {
        try {
          await connection.close();
          break;
        } catch {
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
      }
    }
  }, 30_000);
});
