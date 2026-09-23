/**
 * Runnable entry point: serve the YAML workflow on a local Temporal dev server
 * and run the sample claim through it with the scripted provider (offline —
 * no API key). See README.md for the exact commands.
 */

import { readFileSync } from "node:fs";

import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";

import { buildRuntime, loadYamlSpec } from "../../src/index.js";
import { ScriptedInsuranceProvider } from "./fakes.js";
import { sampleClaim } from "./sample-claim.js";
import { schemas } from "./schemas.js";

async function main(): Promise<void> {
  const spec = loadYamlSpec(readFileSync(new URL("./typeflux.yaml", import.meta.url), "utf-8"), {
    sourceLabel: "examples/insurance-claim-review/typeflux.yaml",
  });
  const address = process.env["TEMPORAL_ADDRESS"] ?? "localhost:7233";
  const workerConnection = await NativeConnection.connect({ address });
  // The starter client must target the SAME server as the worker — a bare
  // `new Client()` would silently default to localhost:7233 (codex).
  const clientConnection = await Connection.connect({ address });
  let runtime: Awaited<ReturnType<typeof buildRuntime>> | undefined;
  try {
    const built = await buildRuntime(spec, {
      provider: new ScriptedInsuranceProvider(),
      schemas,
      worker: { connection: workerConnection },
      // Run the interpreter from source (swap for the dist path in a built app).
      workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
    });
    runtime = built;
    const client = new Client({ connection: clientConnection });
    const packet = await built.worker.runUntil(
      built.runWorkflow(client, sampleClaim(), {
        workflowId: `insurance-claim-review-${Date.now()}`,
      }),
    );
    console.log(JSON.stringify(packet, null, 2));
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
