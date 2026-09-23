/**
 * Runnable entry point for the human-in-the-loop review gate: serve the YAML
 * workflow on a local Temporal dev server, start a case, wait for the workflow
 * to PAUSE at the review gate, submit a decision, and await the routed final
 * decision. Offline apart from the dev server (scripted provider — no API key).
 * See README.md for the commands.
 */

import { readFileSync } from "node:fs";

import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";

import { buildRuntime, loadYamlSpec, YAML_WORKFLOW_TYPE } from "../../src/index.js";
import { LifecycleDemoProvider } from "./fakes.js";
import { hooks } from "./hooks.js";
import { sampleCase } from "./sample-case.js";
import { schemas } from "./schemas.js";

// Which route the "human" picks — one of the spec's user_decisions. Try
// `route_department` or `send_email` to watch different steps get skipped.
const DECISION = process.env["LIFECYCLE_REVIEW_DECISION"] ?? "prepare_submission";

async function main(): Promise<void> {
  const spec = loadYamlSpec(readFileSync(new URL("./typeflux.yaml", import.meta.url), "utf-8"), {
    sourceLabel: "examples/lifecycle-review/typeflux.yaml",
  });
  const address = process.env["TEMPORAL_ADDRESS"] ?? "localhost:7233";
  const workerConnection = await NativeConnection.connect({ address });
  const clientConnection = await Connection.connect({ address });
  let runtime: Awaited<ReturnType<typeof buildRuntime>> | undefined;
  try {
    const built = await buildRuntime(spec, {
      provider: new LifecycleDemoProvider(),
      schemas,
      hooks,
      worker: { connection: workerConnection },
      workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
    });
    runtime = built;
    const client = new Client({ connection: clientConnection });

    await built.worker.runUntil(async () => {
      const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
        taskQueue: built.taskQueue,
        workflowId: `lifecycle-review-${Date.now()}`,
        args: [built.plan, sampleCase()],
      });

      // Wait for the gate to open (the workflow pauses after package_for_review).
      for (let waited = 0; waited < 10_000; waited += 200) {
        const status = (await handle.query("typeflux_lifecycle_status")) as { state: string };
        if (status.state === "waiting_for_review") {
          console.log(`gate open — submitting decision "${DECISION}"`);
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 200));
      }

      // The human decides: route forward (the skipped route steps never run).
      await handle.signal("typeflux_submit_review", { user_decision: DECISION, reviewer: "sam" });
      const decision = await handle.result();
      console.log(JSON.stringify(decision, null, 2));
    });
  } finally {
    // Traces are OUT OF THE BOX: the spec declares langfuse, buildRuntime
    // wires the official SDK from LANGFUSE_* env (absent keys → an untraced
    // run with a notice). Traces stream per activity; drain awaits the tail —
    // even when the run FAILED, the error trace ships.
    await runtime?.drainObservability();
    await clientConnection.close();
    await workerConnection.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
