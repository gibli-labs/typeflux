import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";
import { buildRuntime } from "@typeflux/temporal-yaml";
import { resolveRuntimeTarget } from "@typeflux/yaml/runtime-target";
import { schemas, provider } from "./bindings.mjs";

const { spec, ...options } = resolveRuntimeTarget({
  manifest: fileURLToPath(new URL("typeflux.project.yaml", import.meta.url)),
  workflow: "echo", environment: "temporal", policies: [],
});
const connection = await NativeConnection.connect({address: spec.runtime.temporal.address});
const clientConnection = await Connection.connect({address: spec.runtime.temporal.address});
try {
  const runtime = await buildRuntime(spec, { ...options, schemas, provider, worker: {connection, namespace: spec.runtime.temporal.namespace} });
  const result = await runtime.worker.runUntil(runtime.runWorkflow(
    new Client({connection: clientConnection, namespace: spec.runtime.temporal.namespace}),
    JSON.parse(readFileSync(new URL("input.json", import.meta.url), "utf8")),
    {workflowId: `local-example-${randomUUID()}`},
  ));
  console.log(JSON.stringify(result));
} finally {
  await clientConnection.close();
  await connection.close();
}
