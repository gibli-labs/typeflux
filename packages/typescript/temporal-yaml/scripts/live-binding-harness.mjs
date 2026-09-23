// Live-proof harness (#618 slice 4): a real TS worker + TS-started executions.
//
// Serves `typefluxYamlWorkflow` for a TS-dialect project directory and starts
// executions with the plan-as-argument shape and the identity memo — exactly
// what the ts-plan-argument binding profile describes. The Python control
// plane then operates these executions (status/review/cancel) in
// tests/test_live_ts_binding.py.
//
// Usage: node ts_worker_harness.mjs <projectDir> <workflowId1> [workflowId2…]
//        node ts_worker_harness.mjs <projectDir> --serve-only
// Prints one JSON line per event ({event: "started"|"ready", …}); runs until
// killed. Requires the workspace built (pnpm -r build) and a local Temporal
// dev server on localhost:7233. `--serve-only` runs the WORKER without
// starting anything — the #642 live proof starts executions through the
// PYTHON control plane (subprocess resolver) and only needs the TS worker.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";
import * as z from "zod";

import { startWorkflow } from "@typeflux/temporal-worker";
import {
  YAML_WORKFLOW_TYPE,
  buildRuntime,
  loadYamlSpec,
  workflowIdentityMemo,
  workflowPlanDigest,
} from "@typeflux/temporal-yaml";

const [projectDir, ...rest] = process.argv.slice(2);
const serveOnly = rest.includes("--serve-only");
const workflowIds = rest.filter((arg) => arg !== "--serve-only");
// The modes are EXCLUSIVE: a mixed invocation must not start executions while
// claiming worker-only mode (Bugbot).
if (!projectDir || (serveOnly ? workflowIds.length > 0 : workflowIds.length === 0)) {
  console.error("usage: node ts_worker_harness.mjs <projectDir> (<workflowId…> | --serve-only)");
  process.exit(2);
}

const spec = loadYamlSpec(readFileSync(join(projectDir, "workflow.yaml"), "utf-8"));
const schemas = { "schemas:Item": z.object({ value: z.string() }) };
const provider = {
  providerName: "fake-live",
  structuredCall: () => ({ value: "assessed" }),
};

// Cloud-capable (#672): TEMPORAL_ADDRESS/TEMPORAL_NAMESPACE/TEMPORAL_TLS/
// TEMPORAL_API_KEY select the cluster the WORKER polls (the Temporal Cloud
// live proof); unset, the local dev server as before. The spec's own address
// is irrelevant here — the worker connection is injected.
const workerAddress = process.env.TEMPORAL_ADDRESS ?? "localhost:7233";
const connectOptions = { address: workerAddress };
if (process.env.TEMPORAL_TLS === "true") connectOptions.tls = true;
if (process.env.TEMPORAL_API_KEY) connectOptions.apiKey = process.env.TEMPORAL_API_KEY;
const connection = await NativeConnection.connect(connectOptions);
const workerNamespace = process.env.TEMPORAL_NAMESPACE ?? "default";
const runtime = await buildRuntime(spec, {
  provider,
  schemas,
  worker: { connection, namespace: workerNamespace },
});
// The START client honors the same env-driven cluster as the worker — a
// mixed localhost-client/cloud-worker invocation would dispatch to a cluster
// no worker polls (finder).
const client = process.env.TEMPORAL_ADDRESS
  ? new Client({
      connection: await Connection.connect({
        address: workerAddress,
        ...(process.env.TEMPORAL_TLS === "true" ? { tls: true } : {}),
        ...(process.env.TEMPORAL_API_KEY ? { apiKey: process.env.TEMPORAL_API_KEY } : {}),
      }),
      namespace: workerNamespace,
    })
  : new Client();
const memo = workflowIdentityMemo(spec, workflowPlanDigest(runtime.plan));

const workerRun = runtime.worker.run();

for (const workflowId of workflowIds) {
  await startWorkflow(client, {
    workflowType: YAML_WORKFLOW_TYPE,
    taskQueue: runtime.taskQueue,
    workflowId,
    args: [runtime.plan, { value: "live-proof" }],
    startOptions: { memo },
  });
  console.log(JSON.stringify({ event: "started", workflowId }));
}
console.log(JSON.stringify({ event: "ready" }));

process.on("SIGTERM", () => {
  runtime.worker.shutdown();
});
await workerRun;
await connection.close();
