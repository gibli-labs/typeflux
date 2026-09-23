// Worker-only harness for the operate-tier live test (#563). Runs a real
// `typefluxYamlWorkflow` worker for the canonical conformance TS fixture project on a local
// Temporal dev server, and prints one JSON line per event. UNLIKE live-binding-harness.mjs it does
// NOT start executions — the CP server under test starts them over HTTP; this harness only provides
// the worker that serves them. Spawned by test/live-operate.test.ts.
//
// Usage: node live-operate-worker.mjs <workflowYamlPath> <taskQueue>
// Prints: {"event":"ready","taskQueue":…} once polling, then runs until SIGTERM.
// Requires: pnpm -r build, and a local Temporal dev server on localhost:7233.

import { readFileSync } from "node:fs";

import { NativeConnection } from "@temporalio/worker";

import { buildRuntime, loadYamlSpec } from "@typeflux/temporal-yaml";
// The REAL conformance schemas, straight from the CP server's dist (a hand-approximated copy
// drifted once already: the fixture shapes are {value}-based, and the prompts render {{value}}).
import { CONFORMANCE_SCHEMAS } from "../../temporal-controlplane/dist/http/conformance-schemas.js";

const [workflowYamlPath] = process.argv.slice(2);
if (!workflowYamlPath) {
  console.error("usage: node live-operate-worker.mjs <workflowYamlPath>");
  process.exit(2);
}

const spec = loadYamlSpec(readFileSync(workflowYamlPath, "utf-8"), { env: {} });

// A deterministic fake provider (no real LLM): every fixture schema is {value}-shaped, so one
// superset (with a value-shaped reviews array for AssessmentBatch) validates for all of them —
// zod object schemas strip unknown keys.
const schemas = CONFORMANCE_SCHEMAS;
const provider = {
  providerName: "fake-live",
  structuredCall: () => ({ value: "ok", reviews: [{ value: "ok" }] }),
};

const connection = await NativeConnection.connect({ address: "localhost:7233" });
const runtime = await buildRuntime(spec, { provider, schemas, worker: { connection } });
const workerRun = runtime.worker.run();
console.log(JSON.stringify({ event: "ready", taskQueue: runtime.taskQueue }));

const shutdown = () => runtime.worker.shutdown();
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
await workerRun;
await connection.close();
