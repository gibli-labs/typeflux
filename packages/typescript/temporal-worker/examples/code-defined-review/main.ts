/**
 * Runnable entry point for the CODE-DEFINED (non-YAML) path: wire typed
 * activities to the shipped `reviewWorkflow` / `batchReviewWorkflow`
 * (`../../src/workflows.ts`), serve them on a worker, and run both against a
 * local Temporal dev server. Offline apart from the server (scripted provider).
 * See README.md for the commands.
 */

import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";

import { InlinePromptRegistry } from "@typeflux/temporal";

import { buildTemporalActivities, createTypefluxWorker, executeWorkflow } from "../../src/index.js";
import { classifyDisclosure, substantiateClaim } from "./activities.js";
import { ScriptedReviewProvider } from "./fakes.js";

// The prompts the two activities resolve — the claim text is templated in so the
// scripted provider can flag the unverifiable one.
const registry = new InlinePromptRegistry({
  "review/classify": [{ role: "user", content: "Classify this disclosure:\n{{text}}" }],
  "review/substantiate": [{ role: "user", content: "Substantiate this claim: {{claim}}" }],
});

async function main(): Promise<void> {
  const address = process.env["TEMPORAL_ADDRESS"] ?? "localhost:7233";
  const taskQueue = process.env["TEMPORAL_TASK_QUEUE"] ?? "code-defined-review";
  const workerConnection = await NativeConnection.connect({ address });
  const clientConnection = await Connection.connect({ address });
  try {
    const provider = new ScriptedReviewProvider();
    const activities = buildTemporalActivities([
      { descriptor: classifyDisclosure, options: { provider, registry } },
      { descriptor: substantiateClaim, options: { provider, registry } },
    ]);
    const worker = await createTypefluxWorker({
      taskQueue,
      activities,
      connection: workerConnection,
      workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
    });
    const client = new Client({ connection: clientConnection });

    await worker.runUntil(async () => {
      // reviewWorkflow: classify, then durable bounded fan-out of substantiation.
      const single = await executeWorkflow(client, {
        workflowType: "reviewWorkflow",
        taskQueue,
        workflowId: `review-${Date.now()}`,
        args: [
          {
            text: "Annual financial disclosure for Q4.",
            claims: ["Revenue grew 12%.", "An unverified partnership doubled reach."],
          },
        ],
      });
      console.log("reviewWorkflow →", JSON.stringify(single, null, 2));

      // batchReviewWorkflow: each disclosure runs as a durable CHILD workflow.
      const batch = await executeWorkflow(client, {
        workflowType: "batchReviewWorkflow",
        taskQueue,
        workflowId: `batch-${Date.now()}`,
        args: [
          [
            { text: "Disclosure A.", claims: ["Claim A1."] },
            { text: "Disclosure B.", claims: ["Claim B1.", "An unverified claim B2."] },
          ],
        ],
      });
      console.log("batchReviewWorkflow →", JSON.stringify(batch, null, 2));
    });
  } finally {
    await clientConnection.close();
    await workerConnection.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
