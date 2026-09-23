/**
 * Runnable entry point for the #55 composition showcase: serve the parent YAML
 * workflow (with its pure-YAML sub-workflows resolved through a manifest-style
 * resolver and a code-injected `finalize` activity) on a local Temporal dev server,
 * start a high-priority batch, drive the two review gates, and await the packet.
 * Offline apart from the dev server (scripted provider — no API key). See README.md.
 */

import { readFileSync } from "node:fs";

import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";

import { buildRuntime, loadYamlSpec, projectSubworkflowResolver, YAML_WORKFLOW_TYPE } from "../../src/index.js";
import { extraActivities } from "./activities.js";
import { ClaimsReviewProvider } from "./fakes.js";
import { schemas } from "./schemas.js";

const read = (name: string): string =>
  readFileSync(new URL(`./${name}`, import.meta.url), "utf-8");

/** Resolve the parent's sibling sub-workflows by manifest id (pure-YAML children). */
const CHILDREN: Record<string, string> = {
  claim_triage: read("claim-triage.yaml"),
  escalation_review: read("escalation-review.yaml"),
};

async function main(): Promise<void> {
  const spec = loadYamlSpec(read("typeflux.yaml"), {
    sourceLabel: "examples/claims-review-composition/typeflux.yaml",
  });
  const address = process.env["TEMPORAL_ADDRESS"] ?? "localhost:7233";
  const workerConnection = await NativeConnection.connect({ address });
  const clientConnection = await Connection.connect({ address });
  let runtime: Awaited<ReturnType<typeof buildRuntime>> | undefined;
  try {
    const built = await buildRuntime(spec, {
      provider: new ClaimsReviewProvider(),
      schemas,
      extraActivities,
      subworkflows: projectSubworkflowResolver("claims_review", (id) =>
        CHILDREN[id] !== undefined ? loadYamlSpec(CHILDREN[id]) : undefined,
      ),
      worker: { connection: workerConnection },
      workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
    });
    runtime = built;
    const client = new Client({ connection: clientConnection });

    await built.worker.runUntil(async () => {
      const workflowId = `claims-review-${Date.now()}`;
      const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
        taskQueue: built.taskQueue,
        workflowId,
        args: [
          built.plan,
          { priority: "high", claims: [{ claim_id: "CLM-1", text: "a" }, { claim_id: "CLM-2", text: "b" }] },
        ],
      });

      const waitForGate = async (gateId: string): Promise<void> => {
        for (let waited = 0; waited < 15_000; waited += 200) {
          const status = (await handle.query("typeflux_lifecycle_status")) as {
            waiting_gates?: { gate_id: string }[];
          };
          if ((status.waiting_gates ?? []).some((gate) => gate.gate_id === gateId)) return;
          await new Promise((resolve) => setTimeout(resolve, 200));
        }
        throw new Error(`gate ${gateId} never opened`);
      };

      await waitForGate("intake_gate");
      console.log('intake_gate open — deciding "escalate"');
      await handle.signal("typeflux_submit_review", { user_decision: "escalate", gate: "intake_gate" });
      await waitForGate("compliance_gate");
      console.log('compliance_gate open — deciding "approve"');
      await handle.signal("typeflux_submit_review", { user_decision: "approve", gate: "compliance_gate" });

      const packet = await handle.result();
      console.log("result:", JSON.stringify(packet));
      const child0 = await client.workflow.getHandle(`${workflowId}.triage_all-0`).describe();
      console.log("child:", `${workflowId}.triage_all-0`, child0.memo?.["typeflux_parent_workflow_id"]);
    });
  } finally {
    await runtime?.drainObservability();
    await clientConnection.close();
    await workerConnection.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
