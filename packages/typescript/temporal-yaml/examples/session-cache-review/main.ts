/**
 * Runnable entry point for the session-cache showcase (#478): run a map fan-out
 * whose activity opts into a per-fan-out provider session cache, then print the
 * lifecycle the workflow drove — one prepare, the handle threaded to every item,
 * one release. Offline apart from the dev server (recording provider).
 */

import { readFileSync } from "node:fs";

import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";

import { buildRuntime, loadYamlSpec } from "../../src/index.js";
import { RecordingReferenceCacheProvider } from "./fakes.js";
import { schemas } from "./schemas.js";

async function main(): Promise<void> {
  const spec = loadYamlSpec(readFileSync(new URL("./typeflux.yaml", import.meta.url), "utf-8"), {
    sourceLabel: "examples/session-cache-review/typeflux.yaml",
  });
  const address = process.env["TEMPORAL_ADDRESS"] ?? "localhost:7233";
  const workerConnection = await NativeConnection.connect({ address });
  const clientConnection = await Connection.connect({ address });
  const provider = new RecordingReferenceCacheProvider();
  let runtime: Awaited<ReturnType<typeof buildRuntime>> | undefined;
  try {
    const built = await buildRuntime(spec, {
      provider,
      schemas,
      worker: { connection: workerConnection },
      workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
    });
    runtime = built;
    const client = new Client({ connection: clientConnection });
    const batch = await built.worker.runUntil(
      built.runWorkflow(
        client,
        { items: [{ id: "A", text: "first" }, { id: "B", text: "second" }, { id: "C", text: "third" }] },
        { workflowId: `session-cache-review-${Date.now()}` },
      ),
    );
    console.log("result →", JSON.stringify(batch, null, 2));
    console.log("\nsession-cache lifecycle:");
    console.log(`  prepared once:   ${provider.prepared.length} call(s), identity ${provider.prepared[0]?.slice(0, 12)}…`);
    console.log(`  handle per item: ${JSON.stringify(provider.perItemCacheIds)}`);
    console.log(`  released once:   ${JSON.stringify(provider.released)}`);
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
